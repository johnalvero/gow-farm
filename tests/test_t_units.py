"""T round, unit level: select_character's result, and the bounds in run_accounts."""

import asyncio
import contextlib
import io
import time
import unittest
from types import SimpleNamespace
from unittest import mock as unittest_mock

import gow_support  # noqa: F401 - pins the base URL before claim_common is imported
from gow_support import PASSWORD
from test_s_units import FakePage, FakeResponse, constants, quietly, SELECT_PATH
from test_units import FakePlaywright, accounts

import claim_common

EMAIL = "tunit@x.test"


class Card:
    """A character card, whose class says whether it is the current character"""

    def __init__(self, page, selected, status):
        self.page = page
        self.selected = selected
        self.status = status

    async def get_attribute(self, name):
        return "selected-profile css-1" if self.selected else "css-1"

    async def click(self):
        if self.status:
            self.page.fire("response", FakeResponse(SELECT_PATH, self.status))


class SelectCharacterResult(unittest.TestCase):
    def select(self, selected, status):
        page = FakePage(statuses=())
        card = Card(page, selected, status)
        with constants(SELECT_PROFILE_TIMEOUT_MS=50, SELECTED_PROFILE_TIMEOUT_MS=50,
                       SIGN_IN_START_TIMEOUT_SECONDS=0.01):
            result, error, out = quietly(claim_common.select_character(page, card, EMAIL))
        self.assertIsNone(error, out)
        self.assertEqual(page.listeners(), 0)
        return result, out

    def test_t1_6_ok_response_on_a_non_selected_card(self):
        self.assertIs(self.select(False, 200)[0], True)

    def test_t1_6_non_ok_response_on_a_non_selected_card(self):
        for status in (500, 401, 404):
            with self.subTest(status):
                result, out = self.select(False, status)
                self.assertIs(result, False)
                self.assertIn(f"select-profile failed: HTTP {status}", out)

    def test_t1_6_no_response_on_a_non_selected_card(self):
        result, out = self.select(False, None)
        self.assertIs(result, False)
        self.assertIn("select-profile not observed", out)

    def test_t1_5_selected_card_is_true_whatever_the_response(self):
        for status in (None, 500, 200):
            with self.subTest(status):
                self.assertIs(self.select(True, status)[0], True)

    def test_a_raising_click_still_propagates_and_cleans_up(self):
        page = FakePage(statuses=())
        card = Card(page, False, 200)
        error = claim_common.PlaywrightError("click failed")

        async def click():
            raise error

        card.click = click
        with constants():
            _, raised, _ = quietly(claim_common.select_character(page, card, EMAIL))
        self.assertIs(raised, error)
        self.assertEqual(page.listeners(), 0)


class ProgressKeeping(unittest.TestCase):
    """The Coder's deviation: a fatal-error return keeps progress"""

    def run_account(self, claim):
        import claim_all_characters as script

        class Dialogs:
            async def all(self):
                return []

        class Page:
            def locator(self, selector):
                return Dialogs()

        @contextlib.asynccontextmanager
        async def page_for(browsers, email):
            yield Page()

        account = {"email": EMAIL, "password": PASSWORD, "line_num": 1}
        browsers = SimpleNamespace(pace=None)
        with unittest_mock.patch.object(script, "account_page", page_for), \
                unittest_mock.patch.object(script, "login", unittest_mock.AsyncMock(return_value=True)), \
                unittest_mock.patch.object(script, "claim_for_character", claim), \
                unittest_mock.patch.object(script.os, "makedirs"):
            result, error, out = quietly(script.claim_for_account(browsers, account, 1, 1))
        self.assertIsNone(error, out)
        return account, result, out

    def test_fatal_error_returns_what_was_recorded(self):
        claim = unittest_mock.AsyncMock(side_effect=RuntimeError("boom\nsecond"))
        account, result, out = self.run_account(claim)
        self.assertEqual(result, {"total": 1, "claimed": 0, "already": 0})
        self.assertEqual(account["progress"], result)
        self.assertIsNot(account["progress"], result)
        self.assertIn("✗ Fatal error: boom", out)
        self.assertNotIn("second", out)

    def test_single_character_outcome_lands_in_progress(self):
        for outcome, key in ((claim_common.CLAIMED, "claimed"), (claim_common.ALREADY_CLAIMED, "already")):
            with self.subTest(outcome):
                account, result, _ = self.run_account(unittest_mock.AsyncMock(return_value=outcome))
                self.assertEqual(result[key], 1)
                self.assertEqual(account["progress"], result)

    def test_failed_outcome_counts_nothing(self):
        _, result, _ = self.run_account(unittest_mock.AsyncMock(return_value=claim_common.FAILED))
        self.assertEqual(result, {"total": 1, "claimed": 0, "already": 0})


class Deadline(unittest.TestCase):
    def test_t2_1_values(self):
        self.assertEqual(claim_common.ACCOUNT_DEADLINE_SECONDS, 900)
        self.assertGreater(claim_common.ACCOUNT_CANCEL_GRACE_SECONDS, claim_common.CONTEXT_CLOSE_TIMEOUT_SECONDS)

    def bounded(self, worker, count=1, concurrency=1, stagger=0, deadline=0.2, grace=0.3, fake=None):
        people = accounts(count)
        began = time.monotonic()
        with unittest_mock.patch.object(claim_common, "ACCOUNT_DEADLINE_SECONDS", deadline), \
                unittest_mock.patch.object(claim_common, "ACCOUNT_CANCEL_GRACE_SECONDS", grace):
            options = SimpleNamespace(concurrency=concurrency, stagger=stagger)
            with unittest_mock.patch.object(claim_common, "async_playwright", lambda: fake or FakePlaywright()):
                results, error, out = quietly(claim_common.run_accounts(
                    people, worker, lambda a: ("failed", a["email"]), options))
        self.assertIsNone(error, out)
        return people, results, out, time.monotonic() - began

    def test_hang_is_stopped_and_named(self):
        async def worker(browsers, account, number, total):
            await asyncio.Event().wait()

        people, results, out, seconds = self.bounded(worker)
        self.assertEqual(results, [("failed", "a0@x.test")])
        self.assertTrue(people[0]["timed_out"])
        self.assertRegex(out, r"\[ERROR\] \[a0@x.test\] Account deadline of 0.2s passed, stopping this account")
        self.assertNotIn("abandoning", out)
        self.assertLess(seconds, 1)

    def test_t2_8_worker_that_swallows_cancellation_is_abandoned(self):
        async def worker(browsers, account, number, total):
            if number == 2:
                return "ok"
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(2)      # outlives the grace, then ends with the run

        people, results, out, seconds = self.bounded(worker, count=2, concurrency=2, grace=0.3)
        self.assertEqual(results, [("failed", "a0@x.test"), "ok"])
        self.assertRegex(out, r"\[WARNING\] \[a0@x.test\] Account did not stop, abandoning it")

    def test_t2_8_abandoning_stays_within_the_grace(self):
        finished = {}

        async def worker(browsers, account, number, total):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(5)      # far past the bound, so an unbounded wait shows

        async def main():
            people = accounts(1)
            options = SimpleNamespace(concurrency=1, stagger=0)
            began = time.monotonic()
            results = await claim_common.run_accounts(people, worker, lambda a: "failed", options)
            finished["seconds"] = time.monotonic() - began
            return results

        with unittest_mock.patch.object(claim_common, "ACCOUNT_DEADLINE_SECONDS", 0.1), \
                unittest_mock.patch.object(claim_common, "ACCOUNT_CANCEL_GRACE_SECONDS", 0.2), \
                unittest_mock.patch.object(claim_common, "async_playwright", FakePlaywright):
            result, error, _ = quietly(main())
        self.assertIsNone(error)
        self.assertEqual(result, ["failed"])
        self.assertLessEqual(finished["seconds"], 0.1 + 0.2 + 1)

    def test_t2_8_hung_context_close_stays_within_its_own_limit(self):
        from test_units import FakeContext, FakeHolder

        async def worker(browsers, account, number, total):
            async with claim_common.account_page(FakeHolder(FakeContext(close_hangs=True)), account["email"]):
                await asyncio.Event().wait()

        with unittest_mock.patch.object(claim_common, "CONTEXT_CLOSE_TIMEOUT_SECONDS", 0.2):
            _, results, out, seconds = self.bounded(worker, deadline=0.1, grace=1)
        self.assertEqual(results, [("failed", "a0@x.test")])
        self.assertIn("Error closing browser context: TimeoutError", out)
        self.assertNotIn("abandoning", out)
        self.assertLess(seconds, 0.1 + 0.2 + 0.5)

    def test_t2_8_queueing_does_not_count(self):
        starts = []

        async def worker(browsers, account, number, total):
            starts.append(time.monotonic())
            await asyncio.sleep(0.2)
            return "ok"

        people, results, out, seconds = self.bounded(worker, count=3, concurrency=3, stagger=0.5, deadline=0.3)
        self.assertEqual(results, ["ok", "ok", "ok"])
        self.assertGreaterEqual(starts[2] - starts[0], 0.95)
        self.assertNotIn("deadline", out)
        self.assertFalse(any("timed_out" in person for person in people))

    def test_t2_10_a_worker_at_90_percent_keeps_its_own_result(self):
        async def worker(browsers, account, number, total):
            await asyncio.sleep(0.45)
            return "mine"

        people, results, out, seconds = self.bounded(worker, deadline=0.5, grace=5)
        self.assertEqual(results, ["mine"])
        self.assertNotIn("timed_out", people[0])
        self.assertNotIn("deadline", out)
        self.assertLess(seconds, 1.5)

    def test_a_worker_raising_on_the_way_out_is_read_and_reported_nowhere(self):
        async def worker(browsers, account, number, total):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("failed while stopping")

        _, results, out, _ = self.bounded(worker)
        self.assertEqual(results, [("failed", "a0@x.test")])
        self.assertNotIn("Background error", out)
        self.assertNotIn("failed while stopping", out)

    def test_a_raising_worker_keeps_today_s_path(self):
        async def worker(browsers, account, number, total):
            raise ValueError("first\nsecond")

        people, results, out, _ = self.bounded(worker)
        self.assertEqual(results, [("failed", "a0@x.test")])
        self.assertNotIn("timed_out", people[0])
        self.assertIn("Error processing a0@x.test: first", out)
        self.assertNotIn("second", out)

    def test_deadline_can_be_disabled(self):
        async def worker(browsers, account, number, total):
            await asyncio.sleep(0.2)
            return "ok"

        _, results, out, _ = self.bounded(worker, deadline=None)
        self.assertEqual(results, ["ok"])


class Interrupt(unittest.TestCase):
    """The Coder's deviation: Ctrl+C waits for the worker, up to the grace"""

    def interrupted(self, worker, grace, events=()):
        async def main():
            options = SimpleNamespace(concurrency=1, stagger=0)
            task = asyncio.ensure_future(claim_common.run_accounts(accounts(1), worker, lambda a: None, options))
            await asyncio.sleep(0.1)
            began = time.monotonic()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return "cancelled", time.monotonic() - began, list(events)
            return "finished", time.monotonic() - began, list(events)

        with unittest_mock.patch.object(claim_common, "ACCOUNT_CANCEL_GRACE_SECONDS", grace), \
                unittest_mock.patch.object(claim_common, "async_playwright", FakePlaywright):
            return quietly(main())

    def test_a_worker_that_stops_is_waited_for_and_no_longer(self):
        events = []

        async def worker(browsers, account, number, total):
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.2)
                events.append("closed")

        result, error, _ = self.interrupted(worker, grace=5, events=events)
        self.assertIsNone(error)
        self.assertEqual(result[0], "cancelled")
        self.assertEqual(result[2], ["closed"], "the account has to close before run_accounts gives up")
        self.assertLess(result[1], 1)

    def test_a_worker_that_will_not_stop_holds_ctrl_c_only_for_the_grace(self):
        async def worker(browsers, account, number, total):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(1)

        result, error, _ = self.interrupted(worker, grace=0.2)
        self.assertIsNone(error)
        self.assertEqual(result[0], "cancelled")
        self.assertLess(result[1], 0.2 + 0.5)


if __name__ == "__main__":
    unittest.main()
