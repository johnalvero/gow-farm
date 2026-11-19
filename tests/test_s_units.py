"""S round, unit level: sign-in tracking, listener cleanup, session wait, sanitizer speed."""

import asyncio
import base64
import contextlib
import inspect
import io
import random
import re
import string
import time
import unittest
from collections import defaultdict
from unittest import mock as unittest_mock

import gow_support  # noqa: F401 - pins the base URL before claim_common is imported
from gow_support import PASSWORD

import claim_common

EMAIL = "unit@x.test"
TOKEN_PATH = "https://app.test/api/appcharge/get-token"
CALLBACK_PATH = "https://app.test/api/auth/callback/credentials"
LOGIN_PATH = "https://app.test/api/account/login"
TIMEOUT_WARNING = ("No character dialog and no sign-in {seconds}s after the login response, "
                   "continuing as a single character account")
SELECT_PATH = "https://app.test/api/account/select-profile"


class FakeRequest:
    def __init__(self, url, method="POST", result=None):
        self.url = url
        self.method = method
        self.result = result

    async def response(self):
        if isinstance(self.result, Exception):
            raise self.result
        if self.result == "hang":
            await asyncio.Event().wait()
        return self.result


class FakeResponse:
    def __init__(self, url, status=200, method="POST", body="{}"):
        self.url = url
        self.status = status
        self.ok = 200 <= status < 300
        self.request = FakeRequest(url, method)
        self.body = body

    async def text(self):
        if isinstance(self.body, Exception):
            raise self.body
        if self.body == "hang":
            await asyncio.Event().wait()
        return self.body


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def nth(self, index):
        return FakeLocator(self.page, f"{self.selector}#{index}")

    def locator(self, selector):
        return self

    def or_(self, other):
        return FakeLocator(self.page, f"{self.selector}|{other.selector}")

    async def evaluate(self, *args, **kwargs):
        if self.page.evaluate_error:
            raise self.page.evaluate_error
        return True

    async def fill(self, value):
        if self.page.fill_error:
            raise self.page.fill_error

    async def click(self, **kwargs):
        if self.page.click_error:
            raise self.page.click_error
        await self.page.after_click()

    async def get_attribute(self, name):
        return ""

    async def is_visible(self):
        if self.selector.startswith('button:has-text("Claim'):
            return "|" not in self.selector and self.page.claim_visible
        if "Accept" in self.selector or "onetrust" in self.selector:
            return False
        return self.page.cooldown_visible

    async def wait_for(self, state="visible", timeout=None):
        if "Select the Character" in self.selector:
            behaviour = self.page.dialog
            if isinstance(behaviour, Exception):
                raise behaviour
            if behaviour == "visible":
                return
            await asyncio.sleep(timeout / 1000)
            raise claim_common.PlaywrightTimeoutError("dialog wait timed out")
        if self.page.vault_times_out:
            raise claim_common.PlaywrightTimeoutError("vault wait timed out")


class FakeFrame:
    def __init__(self, url):
        self.url = url


class FakePage:
    """Counts listeners, and like a real page raises KeyError on a repeat removal"""

    def __init__(self, goto_status=200, statuses=(200,), dialog="timeout", fire_on_click=()):
        self.handlers = defaultdict(list)
        self.registered = defaultdict(int)
        self.goto_status = goto_status
        self.goto_errors = []
        self.statuses = list(statuses)
        self.dialog = dialog
        self.fire_on_click = list(fire_on_click)
        self.fill_error = None
        self.click_error = None
        self.evaluate_error = None
        self.claim_visible = True
        self.cooldown_visible = False
        self.vault_times_out = False
        self.gotos = 0
        self.closed = False
        self.main_frame = FakeFrame("https://app.test/en-us/login")
        self.commits = False            # goto fires framenavigated, as a real commit does
        self.on_commit = None           # runs just after that commit

    def on(self, event, handler):
        self.handlers[event].append(handler)
        self.registered[event] += 1

    def remove_listener(self, event, handler):
        if handler not in self.handlers[event]:
            raise KeyError(handler)
        self.handlers[event].remove(handler)

    def listeners(self):
        return sum(len(found) for found in self.handlers.values())

    def fire(self, event, value):
        for handler in list(self.handlers[event]):
            handler(value)

    async def goto(self, url, **kwargs):
        self.gotos += 1
        if self.goto_errors:
            error = self.goto_errors.pop(0)
            if error:
                raise error
        if self.commits:
            self.main_frame.url = url
            self.fire("framenavigated", self.main_frame)
            if self.on_commit:
                self.on_commit(self)
        return FakeResponse(url, self.goto_status, "GET")

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_text(self, text):
        return FakeLocator(self, text)

    def is_closed(self):
        return self.closed

    async def after_click(self):
        step = self.fire_on_click.pop(0) if self.fire_on_click else None
        if step:
            await step(self)
        if self.statuses:
            status = self.statuses.pop(0)
            if status:
                self.fire("response", FakeResponse(LOGIN_PATH, status))


async def sign_in_requested(page):
    page.fire("request", FakeRequest(TOKEN_PATH))


async def sign_in_finished(page):
    page.fire("request", FakeRequest(TOKEN_PATH))
    page.fire("request", FakeRequest(CALLBACK_PATH))
    page.fire("response", FakeResponse(CALLBACK_PATH))


FAST = {
    "LOGIN_DIALOG_GRACE_MS": 10,
    "LOGIN_OUTCOME_TIMEOUT_SECONDS": 0.05,
    "LOGIN_OUTCOME_POLL_MS": 10,
    "LOGIN_RESPONSE_TIMEOUT_MS": 100,
    "LOGIN_RETRY_BACKOFF_SECONDS": 0,
    "SIGN_IN_TIMEOUT_SECONDS": 0.1,
    "SESSION_SETTLE_SECONDS": 0.01,
    "SIGN_IN_START_TIMEOUT_SECONDS": 0.05,
    "SELECT_PROFILE_TIMEOUT_MS": 100,
    "SELECTED_PROFILE_TIMEOUT_MS": 100,
}


@contextlib.contextmanager
def constants(**changes):
    patches = [unittest_mock.patch.object(claim_common, name, value) for name, value in {**FAST, **changes}.items()]
    for patch in patches:
        patch.start()
    try:
        yield
    finally:
        for patch in patches:
            patch.stop()


def quietly(coroutine):
    out = io.StringIO()
    result = None
    error = None
    with contextlib.redirect_stdout(out):
        try:
            result = asyncio.run(coroutine)
        except Exception as e:
            error = e
    return result, error, out.getvalue()


def run_login(page, **changes):
    with constants(**changes):
        return quietly(claim_common.login(page, EMAIL, PASSWORD))


def run_select(page, **changes):
    with constants(**changes):
        return quietly(claim_common.select_character(page, page.locator("card"), EMAIL))


class TrackSignIn(unittest.TestCase):
    """S1.5"""

    def test_flags(self):
        async def main():
            page = FakePage()
            state = claim_common.track_sign_in(page)

            page.fire("request", FakeRequest("https://app.test/api/other"))
            page.fire("response", FakeResponse("https://app.test/api/other"))
            self.assertFalse(state["started"])
            self.assertFalse(state["done"].is_set())

            page.fire("request", FakeRequest(TOKEN_PATH))
            self.assertTrue(state["started"])
            self.assertFalse(state["done"].is_set())
            page.fire("response", FakeResponse(TOKEN_PATH))
            self.assertFalse(state["done"].is_set())

            page.fire("response", FakeResponse(CALLBACK_PATH))
            self.assertTrue(state["done"].is_set())
            state["stop"]()

        asyncio.run(main())

    def test_callback_request_alone_starts_it(self):
        async def main():
            page = FakePage()
            state = claim_common.track_sign_in(page)
            page.fire("request", FakeRequest(CALLBACK_PATH + "?callbackUrl=x"))
            self.assertTrue(state["started"])
            self.assertFalse(state["done"].is_set())
            state["stop"]()

        asyncio.run(main())

    def test_stop_is_idempotent_on_a_page_that_raises_on_repeat_removal(self):
        async def main():
            page = FakePage()
            state = claim_common.track_sign_in(page)
            state["stop"]()
            state["stop"]()
            self.assertEqual(page.listeners(), 0)

        asyncio.run(main())


class WaitForSignIn(unittest.TestCase):
    def state(self):
        return {"started": False, "stopped": False, "done": asyncio.Event(), "stop": unittest_mock.Mock()}

    def test_start_timeout_defaults_to_zero(self):
        self.assertEqual(inspect.signature(claim_common.wait_for_sign_in).parameters["start_timeout"].default, 0)

    def test_start_timeout_runs_out_silently(self):
        async def main():
            state = self.state()
            began = time.monotonic()
            await claim_common.wait_for_sign_in(state, EMAIL, start_timeout=0.3)
            self.assertGreaterEqual(time.monotonic() - began, 0.3)
            self.assertLess(time.monotonic() - began, 0.8)
            state["stop"].assert_called_once()

        _, error, out = quietly(main())
        self.assertIsNone(error)
        self.assertEqual(out, "")

    def test_never_started_returns_at_once_with_no_warning(self):
        """S1.4"""
        async def main():
            state = self.state()
            began = time.monotonic()
            await claim_common.wait_for_sign_in(state, EMAIL)
            self.assertLess(time.monotonic() - began, 0.1)
            state["stop"].assert_called_once()

        _, error, out = quietly(main())
        self.assertIsNone(error)
        self.assertEqual(out, "")

    def test_timeout_warns_with_the_email(self):
        async def main():
            state = self.state()
            state["started"] = True
            with unittest_mock.patch.object(claim_common, "SIGN_IN_TIMEOUT_SECONDS", 0.05):
                await claim_common.wait_for_sign_in(state, EMAIL)

        _, error, out = quietly(main())
        self.assertIsNone(error)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] Sign-in did not finish in 0.05s, continuing")


class LoginListeners(unittest.TestCase):
    """S2.1, S2.2, S2.5: no track_sign_in listener outlives login, on any path"""

    def assert_clean(self, page, error=None, returned=None):
        result, raised, out = run_login(page)
        self.assertEqual(page.listeners(), 0, out)
        if error:
            self.assertIs(raised, error)
        else:
            self.assertIsNone(raised, out)
            self.assertEqual(result, returned, out)
        return out

    def test_dialog_appears(self):
        self.assert_clean(FakePage(dialog="visible"), returned=True)

    def test_dialog_times_out_with_sign_in_done(self):
        self.assert_clean(FakePage(fire_on_click=[sign_in_finished]), returned=True)

    def test_dialog_times_out_with_sign_in_unstarted(self):
        out = self.assert_clean(FakePage(), returned=True)
        self.assertNotIn("Sign-in", out)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] {TIMEOUT_WARNING.format(seconds='0.05')}")

    def test_sign_in_timeout_expires(self):
        out = self.assert_clean(FakePage(fire_on_click=[sign_in_requested]), returned=True)
        self.assertIn("Sign-in did not finish", out)

    def test_page_load_failure(self):
        self.assert_clean(FakePage(goto_status=503), returned=False)

    def test_login_rejected_401(self):
        out = self.assert_clean(FakePage(statuses=(401,)), returned=False)
        self.assertIn("Login rejected: HTTP 401", out)

    def test_second_5xx(self):
        page = FakePage(statuses=(500, 503))
        out = self.assert_clean(page, returned=False)
        self.assertEqual(page.gotos, 2)
        self.assertIn("Login rejected: HTTP 503", out)

    def test_goto_raises(self):
        page, error = FakePage(), claim_common.PlaywrightError("goto failed")
        page.goto_errors = [error]
        self.assert_clean(page, error=error)

    def test_goto_raises_on_the_second_attempt(self):
        page, error = FakePage(statuses=(500,)), claim_common.PlaywrightError("goto failed")
        page.goto_errors = [None, error]
        self.assert_clean(page, error=error)

    def test_fill_raises(self):
        page, error = FakePage(), claim_common.PlaywrightError("fill failed")
        page.fill_error = error
        self.assert_clean(page, error=error)

    def test_click_raises(self):
        page, error = FakePage(), claim_common.PlaywrightError("click failed")
        page.click_error = error
        self.assert_clean(page, error=error)

    def test_dialog_wait_raises_something_other_than_a_timeout(self):
        error = claim_common.PlaywrightError("target closed")
        self.assert_clean(FakePage(dialog=error), error=error)

    def test_non_playwright_exception(self):
        page, error = FakePage(), RuntimeError("unexpected")
        page.fill_error = error
        self.assert_clean(page, error=error)

    def test_no_observed_response(self):
        page = FakePage(statuses=(None,))
        out = self.assert_clean(page, returned=True)
        self.assertIn("Login response not observed", out)

    def test_retry_registers_one_pair_of_sign_in_listeners(self):
        page = FakePage(statuses=(500, 200))
        real = claim_common.track_sign_in
        with unittest_mock.patch.object(claim_common, "track_sign_in", side_effect=real) as track:
            self.assert_clean(page, returned=True)
        track.assert_called_once()

    def test_sign_in_starting_on_the_second_attempt_is_still_waited_for(self):
        async def late(page):
            page.fire("request", FakeRequest(TOKEN_PATH))

            async def finish():
                await asyncio.sleep(0.3)
                page.fire("response", FakeResponse(CALLBACK_PATH))

            page.finisher = asyncio.ensure_future(finish())

        async def nothing(page):
            pass

        page = FakePage(statuses=(500, 200), fire_on_click=[nothing, late])
        began = time.monotonic()
        result, error, out = run_login(page, SIGN_IN_TIMEOUT_SECONDS=5)
        self.assertIsNone(error)
        self.assertTrue(result)
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertNotIn("Sign-in did not finish", out)
        self.assertEqual(page.listeners(), 0)

    def test_login_never_waits_for_a_late_start(self):
        """S1 keeps login at the default of 0, whatever the selection bound is"""
        began = time.monotonic()
        result, _, out = run_login(FakePage(), LOGIN_OUTCOME_TIMEOUT_SECONDS=0.1, SIGN_IN_START_TIMEOUT_SECONDS=30)
        self.assertTrue(result)
        self.assertLess(time.monotonic() - began, 0.1 + 0.5)
        self.assertNotIn("Sign-in", out)

    def test_unstarted_sign_in_ends_within_half_a_second_of_the_deadline(self):
        """S1.4"""
        began = time.monotonic()
        result, _, out = run_login(FakePage(), LOGIN_OUTCOME_TIMEOUT_SECONDS=0.3)
        self.assertTrue(result)
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertLess(time.monotonic() - began, 0.3 + 0.5)
        self.assertNotIn("Sign-in", out)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] {TIMEOUT_WARNING.format(seconds='0.3')}")


class LateDialogLocator(FakeLocator):
    """Waits for the dialog as Playwright does: back as soon as it shows, or a timeout"""

    async def wait_for(self, state="visible", timeout=None):
        if "Select the Character" not in self.selector:
            return await super().wait_for(state, timeout)
        page = self.page
        page.dialog_timeouts.append(timeout)
        if isinstance(page.dialog_error, Exception):
            raise page.dialog_error
        ends = time.monotonic() + timeout / 1000
        while not page.dialog_shown():
            left = ends - time.monotonic()
            if left <= 0:
                raise claim_common.PlaywrightTimeoutError("dialog wait timed out")
            await asyncio.sleep(min(left, 0.005))


class LateDialogPage(FakePage):
    """A FakePage whose dialog turns visible dialog_after seconds after the Login click"""

    def __init__(self, dialog_after=None, **kwargs):
        super().__init__(**kwargs)
        self.dialog_after = dialog_after
        self.clicked_at = None
        self.dialog_timeouts = []
        self.dialog_error = None

    def dialog_shown(self):
        return (self.dialog_after is not None and self.clicked_at is not None
                and time.monotonic() - self.clicked_at >= self.dialog_after)

    def locator(self, selector):
        return LateDialogLocator(self, selector)

    async def after_click(self):
        self.clicked_at = time.monotonic()
        await super().after_click()


def sign_in_finished_with(status):
    async def step(page):
        page.fire("request", FakeRequest(TOKEN_PATH))
        page.fire("request", FakeRequest(CALLBACK_PATH))
        page.fire("response", FakeResponse(CALLBACK_PATH, status))
    return step


def sign_in_finishing_after(seconds):
    async def step(page):
        page.fire("request", FakeRequest(TOKEN_PATH))

        async def finish():
            await asyncio.sleep(seconds)
            page.fire("response", FakeResponse(CALLBACK_PATH))

        page.finisher = asyncio.ensure_future(finish())
    return step


def after_click(out, text):
    """Seconds in the "... {s}s after the Login click" line that follows text"""
    found = re.search(rf"{re.escape(text)} (\d+\.\d)s after the Login click", out)
    return float(found.group(1)) if found else None


class LoginOutcome(unittest.TestCase):
    """LR8: login() races the dialog against a finished sign-in, with fakes only"""

    def timed_login(self, page, **changes):
        began = time.monotonic()
        result, error, out = run_login(page, **changes)
        seconds = time.monotonic() - began
        self.assertIsNone(error, out)
        self.assertEqual(page.listeners(), 0, out)     # LR8 (e), on every case
        return result, out, seconds

    def test_a_late_dialog_ends_the_wait_as_soon_as_it_shows(self):
        """LR8 (a)"""
        page = LateDialogPage(dialog_after=0.2)
        result, out, seconds = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2)
        self.assertIs(result, True)
        self.assertLess(seconds, 0.7, out)
        self.assertGreaterEqual(seconds, 0.2, out)
        self.assertRegex(out, rf"\[INFO\] \[{EMAIL}\] Character dialog appeared ")
        self.assertGreaterEqual(after_click(out, "Character dialog appeared"), 0.2)
        self.assertNotIn("Signed in with no character dialog", out)
        self.assertNotIn("WARNING", out)

    def test_a_finished_sign_in_with_no_dialog_is_single_character(self):
        """LR8 (b)"""
        page = LateDialogPage(fire_on_click=[sign_in_finished])
        result, out, seconds = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2)
        self.assertIs(result, True)
        self.assertRegex(out, rf"\[INFO\] \[{EMAIL}\] Signed in with no character dialog ")
        self.assertNotIn("WARNING", out)
        self.assertNotIn("Character dialog appeared", out)
        self.assertLess(seconds, 0.5, out)

    def test_nothing_at_all_waits_out_the_timeout_and_warns(self):
        """LR8 (c)"""
        result, out, seconds = self.timed_login(LateDialogPage(), LOGIN_OUTCOME_TIMEOUT_SECONDS=0.3)
        self.assertIs(result, True)
        self.assertGreaterEqual(seconds, 0.3, out)
        self.assertLess(seconds, 0.8, out)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] {TIMEOUT_WARNING.format(seconds='0.3')}")
        self.assertNotIn("Sign-in", out)

    def test_a_dialog_during_the_grace_after_sign_in_is_multi_character(self):
        """LR8 (d)"""
        page = LateDialogPage(dialog_after=0.15, fire_on_click=[sign_in_finished])
        result, out, _ = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2, LOGIN_DIALOG_GRACE_MS=1000)
        self.assertIs(result, True)
        self.assertIn("Signed in with no character dialog", out)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] Character dialog appeared after sign-in, "
                              "treating as multi-character")

    def test_a_dialog_after_the_grace_is_not_seen(self):
        """The other side of (d): the grace is a bound, not a wait for ever"""
        page = LateDialogPage(dialog_after=0.4, fire_on_click=[sign_in_finished])
        result, out, seconds = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2, LOGIN_DIALOG_GRACE_MS=100)
        self.assertIs(result, True)
        self.assertNotIn("treating as multi-character", out)
        self.assertNotIn("WARNING", out)
        self.assertLess(seconds, 0.4, out)

    def test_a_zero_grace_never_waits_for_ever(self):
        """Playwright reads a 0 timeout as no limit, so the grace wait must never pass one"""
        page = LateDialogPage(fire_on_click=[sign_in_finished])
        result, out, seconds = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2, LOGIN_DIALOG_GRACE_MS=0)
        self.assertIs(result, True)
        self.assertLess(seconds, 0.5, out)
        self.assertTrue(page.dialog_timeouts, out)
        self.assertNotIn(0, page.dialog_timeouts)
        self.assertTrue(all(t >= 1 for t in page.dialog_timeouts), page.dialog_timeouts)

    def test_no_dialog_wait_is_ever_given_a_zero_or_negative_timeout(self):
        """Polled in slices no longer than the poll, and the last slice near the deadline is still >= 1ms"""
        page = LateDialogPage()
        result, _, _ = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=0.1, LOGIN_OUTCOME_POLL_MS=7)
        self.assertIs(result, True)
        self.assertGreater(len(page.dialog_timeouts), 3)
        self.assertTrue(all(1 <= t <= 7 for t in page.dialog_timeouts), page.dialog_timeouts)

    def test_a_visible_dialog_wins_over_a_finished_sign_in(self):
        page = LateDialogPage(dialog_after=0, fire_on_click=[sign_in_finished])
        result, out, _ = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2)
        self.assertIs(result, True)
        self.assertIn("Character dialog appeared", out)
        self.assertNotIn("Signed in with no character dialog", out)

    def test_a_sign_in_finishing_later_ends_the_race_without_the_timeout(self):
        page = LateDialogPage(fire_on_click=[sign_in_finishing_after(0.2)])
        result, out, seconds = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=5, SIGN_IN_TIMEOUT_SECONDS=5)
        self.assertIs(result, True)
        self.assertIn("Signed in with no character dialog", out)
        self.assertGreaterEqual(after_click(out, "Signed in with no character dialog"), 0.2)
        self.assertLess(seconds, 1.0, out)
        self.assertNotIn("WARNING", out)

    def test_a_started_sign_in_that_outlasts_the_race_gives_no_timeout_warning(self):
        """The plan: if sign-in has started, wait_for_sign_in reports it, not the new line"""
        page = LateDialogPage(fire_on_click=[sign_in_requested])
        result, out, _ = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=0.1, SIGN_IN_TIMEOUT_SECONDS=0.1)
        self.assertIs(result, True)
        self.assertNotIn("No character dialog and no sign-in", out)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] Sign-in did not finish in 0.1s, continuing")

    def test_a_refused_sign_in_is_reported(self):
        for status in (401, 500, 302, 199):
            with self.subTest(status=status):
                page = LateDialogPage(fire_on_click=[sign_in_finished_with(status)])
                result, out, _ = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2)
                self.assertIs(result, True)
                self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] Sign-in callback returned HTTP {status}")

    def test_an_ok_sign_in_is_not_reported(self):
        for status in (200, 204, 299):
            with self.subTest(status=status):
                page = LateDialogPage(fire_on_click=[sign_in_finished_with(status)])
                _, out, _ = self.timed_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2)
                self.assertNotIn("Sign-in callback returned", out)

    def test_only_the_first_callback_is_judged(self):
        async def two(page):
            page.fire("request", FakeRequest(TOKEN_PATH))
            page.fire("response", FakeResponse(CALLBACK_PATH, 401))
            page.fire("response", FakeResponse(CALLBACK_PATH, 200))

        _, out, _ = self.timed_login(LateDialogPage(fire_on_click=[two]), LOGIN_OUTCOME_TIMEOUT_SECONDS=2)
        self.assertIn("Sign-in callback returned HTTP 401", out)

    def test_track_sign_in_starts_with_no_status_and_keeps_the_first(self):
        async def main():
            page = FakePage()
            state = claim_common.track_sign_in(page)
            self.assertIsNone(state["status"])
            page.fire("response", FakeResponse(CALLBACK_PATH, 403))
            page.fire("response", FakeResponse(CALLBACK_PATH, 200))
            self.assertEqual(state["status"], 403)
            self.assertTrue(state["done"].is_set())
            state["stop"]()

        asyncio.run(main())

    def test_a_dialog_wait_error_still_propagates(self):
        page, error = LateDialogPage(), claim_common.PlaywrightError("target closed")
        page.dialog_error = error
        result, raised, out = run_login(page, LOGIN_OUTCOME_TIMEOUT_SECONDS=2)
        self.assertIs(raised, error)
        self.assertEqual(page.listeners(), 0, out)

    def test_signature(self):
        """LR8 (f)"""
        self.assertEqual(list(inspect.signature(claim_common.login).parameters), ["page", "email", "password", "pace"])

    def test_constants(self):
        """LR8 (g)"""
        self.assertEqual(claim_common.LOGIN_OUTCOME_TIMEOUT_SECONDS, 30)
        self.assertEqual(claim_common.LOGIN_DIALOG_GRACE_MS, 3000)


class SelectCharacterListeners(unittest.TestCase):
    """S2.3, S1.4"""

    def page(self, status, **kwargs):
        page = FakePage(statuses=(), **kwargs)

        async def respond(page):
            if status:
                page.fire("response", FakeResponse(SELECT_PATH, status))

        page.fire_on_click = [respond]
        return page

    def test_click_raises(self):
        page, error = self.page(200), claim_common.PlaywrightError("click failed")
        page.click_error = error
        _, raised, _ = run_select(page)
        self.assertIs(raised, error)
        self.assertEqual(page.listeners(), 0)

    def test_ok_response(self):
        page = self.page(200)
        _, raised, out = run_select(page)
        self.assertIsNone(raised, out)
        self.assertEqual(page.listeners(), 0)
        self.assertNotIn("Sign-in", out)

    def test_no_response(self):
        page = self.page(None)
        _, raised, out = run_select(page)
        self.assertIsNone(raised, out)
        self.assertEqual(page.listeners(), 0)
        self.assertIn("select-profile not observed", out)

    def test_sign_in_timeout(self):
        async def respond(page):
            page.fire("response", FakeResponse(SELECT_PATH, 200))
            page.fire("request", FakeRequest(TOKEN_PATH))

        page = FakePage(statuses=(), fire_on_click=[respond])
        _, raised, out = run_select(page)
        self.assertIsNone(raised, out)
        self.assertEqual(page.listeners(), 0)
        self.assertIn("Sign-in did not finish", out)

    def timed_select(self, page, **changes):
        changes = {"SESSION_SETTLE_SECONDS": 0.2, **changes}
        began = time.monotonic()
        with constants(**changes):
            _, raised, out = quietly(claim_common.select_character(page, page.locator("card"), EMAIL))
        self.assertIsNone(raised, out)
        self.assertEqual(page.listeners(), 0)
        return time.monotonic() - began, out

    def test_no_successful_post_adds_only_the_settle(self):
        """S1.4's first bound, and S1.8: no start wait at all, however long it is set"""
        for status in (None, 500):
            with self.subTest(status=status):
                seconds, out = self.timed_select(self.page(status), SIGN_IN_START_TIMEOUT_SECONDS=30,
                                                 SELECT_PROFILE_TIMEOUT_MS=50)
                self.assertLessEqual(seconds, 0.2 + 0.05 + 0.5)
                self.assertNotIn("Sign-in", out)
                self.assertIn("select-profile", out)

    def test_successful_post_with_no_sign_in_waits_the_start_bound_silently(self):
        """S1.4's second bound, and S1.8"""
        seconds, out = self.timed_select(self.page(200), SIGN_IN_START_TIMEOUT_SECONDS=0.4)
        self.assertGreaterEqual(seconds, 0.2 + 0.4)
        self.assertLessEqual(seconds, 0.2 + 0.4 + 0.5)
        self.assertEqual(out, "")

    def test_default_start_bound(self):
        self.assertEqual(claim_common.SIGN_IN_START_TIMEOUT_SECONDS, 3)

    def test_sign_in_within_the_settle_pays_nothing_extra(self):
        """S1.8"""
        async def respond(page):
            page.fire("response", FakeResponse(SELECT_PATH, 200))
            await sign_in_finished(page)

        page = FakePage(statuses=(), fire_on_click=[respond])
        seconds, out = self.timed_select(page, SIGN_IN_START_TIMEOUT_SECONDS=30, SIGN_IN_TIMEOUT_SECONDS=30)
        self.assertLessEqual(seconds, 0.2 + 0.1)
        self.assertEqual(out, "")

    def test_sign_in_starting_inside_the_start_bound_is_waited_to_the_end(self):
        async def respond(page):
            page.fire("response", FakeResponse(SELECT_PATH, 200))

            async def later():
                await asyncio.sleep(0.4)
                page.fire("request", FakeRequest(TOKEN_PATH))
                await asyncio.sleep(0.3)
                page.fire("response", FakeResponse(CALLBACK_PATH))

            page.later = asyncio.ensure_future(later())

        page = FakePage(statuses=(), fire_on_click=[respond])
        seconds, out = self.timed_select(page, SESSION_SETTLE_SECONDS=0.05, SIGN_IN_START_TIMEOUT_SECONDS=2,
                                         SIGN_IN_TIMEOUT_SECONDS=5)
        self.assertGreaterEqual(seconds, 0.7)
        self.assertLess(seconds, 1.5)
        self.assertEqual(out, "")

    def test_timeouts_still_apply_after_a_late_start(self):
        async def respond(page):
            page.fire("response", FakeResponse(SELECT_PATH, 200))

            async def later():
                await asyncio.sleep(0.1)
                page.fire("request", FakeRequest(TOKEN_PATH))

            page.later = asyncio.ensure_future(later())

        page = FakePage(statuses=(), fire_on_click=[respond])
        _, out = self.timed_select(page, SESSION_SETTLE_SECONDS=0.01, SIGN_IN_START_TIMEOUT_SECONDS=2,
                                   SIGN_IN_TIMEOUT_SECONDS=0.2)
        self.assertIn("Sign-in did not finish", out)


class WaitForSession(unittest.TestCase):
    """S3.4, S3.8"""

    def run_wait(self, requests, **changes):
        changes = {"SESSION_SETTLE_SECONDS": 0.01, "SESSION_TIMEOUT_SECONDS": 0.2,
                   "SESSION_REQUEST_TIMEOUT_SECONDS": 0.2, **changes}
        began = time.monotonic()
        with constants(**changes):
            _, error, out = quietly(claim_common.wait_for_session(requests, EMAIL))
        return error, out, time.monotonic() - began

    def session(self, result):
        return FakeRequest("https://app.test/api/auth/session", "GET", result)

    def test_error_from_the_request_logs_its_first_line_and_continues(self):
        error, out, _ = self.run_wait([self.session(claim_common.PlaywrightError("first\nsecond"))])
        self.assertIsNone(error)
        self.assertIn(f"[{EMAIL}] Session load not confirmed: first", out)
        self.assertNotIn("second", out)

    def test_error_from_the_body_logs_its_first_line_and_continues(self):
        body = claim_common.PlaywrightError("body first\nbody second")
        error, out, _ = self.run_wait([self.session(FakeResponse("u", 200, "GET", body))])
        self.assertIsNone(error)
        self.assertIn("Session load not confirmed: body first", out)
        self.assertNotIn("body second", out)

    def test_hung_body_is_bounded(self):
        error, out, seconds = self.run_wait([self.session(FakeResponse("u", 200, "GET", "hang"))])
        self.assertIsNone(error)
        self.assertIn("Session load not confirmed: TimeoutError", out)
        self.assertLessEqual(seconds, 2 * 0.2 + 1.5)

    def test_slow_response_then_hung_body_stays_within_twice_the_limit(self):
        class Slow(FakeRequest):
            async def response(self):
                await asyncio.sleep(0.15)
                return FakeResponse("u", 200, "GET", "hang")

        error, _, seconds = self.run_wait([Slow("https://app.test/api/auth/session", "GET")])
        self.assertIsNone(error)
        self.assertLessEqual(seconds, 2 * 0.2 + 1.5)

    def test_no_response_object_logs_not_confirmed(self):
        error, out, _ = self.run_wait([self.session(None)])
        self.assertIsNone(error)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] Session load not confirmed")

    def test_no_request_warns_after_the_limit(self):
        error, out, seconds = self.run_wait([])
        self.assertIsNone(error)
        self.assertRegex(out, rf"\[WARNING\] \[{EMAIL}\] No session request seen, continuing")
        self.assertGreaterEqual(seconds, 0.2)
        self.assertLessEqual(seconds, 0.2 + 1.5)

    def test_a_late_request_ends_the_wait_and_is_the_one_used(self):
        requests = []

        async def main():
            async def arrive():
                await asyncio.sleep(0.2)
                requests.append(self.session(FakeResponse("u", 200, "GET", '{"user":1}')))

            arriving = asyncio.ensure_future(arrive())
            await claim_common.wait_for_session(requests, EMAIL)
            await arriving

        began = time.monotonic()
        with constants(SESSION_SETTLE_SECONDS=0.01, SESSION_REQUEST_TIMEOUT_SECONDS=5):
            _, error, out = quietly(main())
        self.assertIsNone(error)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual(out, "")

    def test_a_started_request_pays_nothing_extra(self):
        ready = [self.session(FakeResponse("u", 200, "GET", '{"user":1}'))]
        error, out, seconds = self.run_wait(ready, SESSION_REQUEST_TIMEOUT_SECONDS=5)
        self.assertIsNone(error)
        self.assertLess(seconds, 1)
        self.assertEqual(out, "")

    def test_empty_bodies_warn(self):
        for body in ("", "{}", "null", " {} ", "\n"):
            with self.subTest(body):
                error, out, _ = self.run_wait([self.session(FakeResponse("u", 200, "GET", body))])
                self.assertIsNone(error)
                self.assertIn("App session is empty, so the claim will not work", out)

    def test_the_latest_request_is_the_one_read(self):
        stale = self.session(claim_common.PlaywrightError("stale"))
        latest = self.session(FakeResponse("u", 200, "GET", '{"user":1}'))
        error, out, _ = self.run_wait([stale, latest])
        self.assertIsNone(error)
        self.assertEqual(out, "")


class VaultListeners(unittest.TestCase):
    """S3.5, S4.2: vault_state leaves no request listener on any path"""

    def run_vault(self, page, **changes):
        changes = {"VAULT_RETRY_DELAY_SECONDS": 0, "SESSION_SETTLE_SECONDS": 0.01, **changes}
        with constants(**changes):
            return quietly(claim_common.vault_state(page, EMAIL))

    def checked(self, page, expected=None, error=None, wait=None):
        wait = wait or unittest_mock.AsyncMock()
        with unittest_mock.patch.object(claim_common, "wait_for_session", wait):
            result, raised, out = self.run_vault(page)
        self.assertEqual(page.listeners(), 0, out)
        self.assertEqual(page.registered["request"], 1)
        self.assertEqual(page.registered["framenavigated"], 1)
        if error:
            self.assertIs(raised, error)
        else:
            self.assertIsNone(raised, out)
            self.assertEqual(result, expected)
        return wait

    def test_claim(self):
        self.checked(FakePage(), "claim").assert_awaited_once()

    def test_cooldown_skips_the_session_wait(self):
        page = FakePage()
        page.claim_visible = False
        page.cooldown_visible = True
        self.checked(page, "cooldown").assert_not_awaited()

    def test_unknown(self):
        page = FakePage()
        page.vault_times_out = True
        self.checked(page, "unknown").assert_not_awaited()

    def test_goto_raises(self):
        page, error = FakePage(), claim_common.PlaywrightError("connection refused")
        page.goto_errors = [error]
        self.checked(page, error=error)

    def test_goto_timeout_is_not_retried(self):
        page, error = FakePage(), claim_common.PlaywrightTimeoutError("Timeout")
        page.goto_errors = [error]
        self.checked(page, error=error)
        self.assertEqual(page.gotos, 1)

    def test_second_abort_raises(self):
        first = claim_common.PlaywrightError("net::ERR_ABORTED")
        second = claim_common.PlaywrightError("net::ERR_ABORTED")
        page = FakePage()
        page.goto_errors = [first, second]
        self.checked(page, error=second)
        self.assertEqual(page.gotos, 2)

    def test_wait_interactive_raises_something_unexpected(self):
        page, error = FakePage(), RuntimeError("unexpected")
        with unittest_mock.patch.object(claim_common, "wait_interactive", side_effect=error):
            self.checked(page, error=error)

    def test_wait_for_session_raises_something_unexpected(self):
        page, error = FakePage(), RuntimeError("unexpected")
        self.checked(page, error=error, wait=unittest_mock.AsyncMock(side_effect=error))


class VaultNavigation(unittest.TestCase):
    """S3.10: only requests made by the document the claim runs on count"""

    SESSION = "https://app.test/api/auth/session"
    VAULT = claim_common.VAULT_URL

    def requests_seen(self, page):
        """What wait_for_session receives"""
        seen = []

        async def capture(session_requests, email):
            seen.extend(session_requests)

        with unittest_mock.patch.object(claim_common, "wait_for_session", capture):
            with constants(VAULT_RETRY_DELAY_SECONDS=0):
                result, error, out = quietly(claim_common.vault_state(page, EMAIL))
        self.assertIsNone(error, out)
        self.assertEqual(result, "claim")
        self.assertEqual(page.listeners(), 0)
        return seen

    def page(self, on_commit=None):
        page = FakePage()
        page.commits = True
        page.on_commit = on_commit
        return page

    def fetch(self, page):
        request = FakeRequest(self.SESSION, "GET")
        page.fire("request", request)
        return request

    def stale_before_goto(self, page):
        real_goto = page.goto

        async def goto(url, **kwargs):
            self.stale = self.fetch(page)
            return await real_goto(url, **kwargs)

        page.goto = goto

    def test_a_request_from_the_page_before_the_commit_is_dropped(self):
        page = self.page(lambda p: setattr(self, "own", self.fetch(p)))
        self.stale_before_goto(page)
        self.assertEqual(self.requests_seen(page), [self.own])

    def test_a_same_path_reload_still_clears(self):
        """The normal multi-character flow: the vault loads over the vault"""
        page = self.page(lambda p: setattr(self, "own", self.fetch(p)))
        page.main_frame.url = self.VAULT
        self.stale_before_goto(page)
        self.assertEqual(self.requests_seen(page), [self.own])

    def test_an_abort_keeps_nothing_from_before_the_retry_commits(self):
        page = self.page(lambda p: setattr(self, "own", self.fetch(p)))
        page.main_frame.url = self.VAULT
        self.stale_before_goto(page)
        page.goto_errors = [claim_common.PlaywrightError("net::ERR_ABORTED")]
        self.assertEqual(self.requests_seen(page), [self.own])

    def test_an_attempt_that_commits_and_then_aborts_is_not_kept(self):
        """The retry lands on the same path, so only resetting per attempt clears this"""
        page = self.page()
        page.main_frame.url = self.VAULT
        real_goto = page.goto
        attempts = []

        async def goto(url, **kwargs):
            attempts.append(url)
            await real_goto(url, **kwargs)
            if len(attempts) == 1:
                self.fetch(page)
                raise claim_common.PlaywrightError("net::ERR_ABORTED")
            self.own = self.fetch(page)

        page.goto = goto
        self.assertEqual(self.requests_seen(page), [self.own])
        self.assertEqual(len(attempts), 2)

    def test_same_document_url_change_on_the_same_path_keeps_the_request(self):
        def commit(page):
            self.own = self.fetch(page)
            page.main_frame.url = self.VAULT + "?tab=1"
            page.fire("framenavigated", page.main_frame)

        self.assertEqual(self.requests_seen(self.page(commit)), [self.own])

    def test_a_subframe_navigating_keeps_the_request(self):
        def commit(page):
            self.own = self.fetch(page)
            page.fire("framenavigated", FakeFrame("https://ads.test/frame"))

        self.assertEqual(self.requests_seen(self.page(commit)), [self.own])

    def test_a_change_of_path_clears(self):
        """The plan's accepted risk, pinned: the app rewriting to another path loses the request"""
        def commit(page):
            self.fetch(page)
            page.main_frame.url = "https://app.test/en-us/elsewhere"
            page.fire("framenavigated", page.main_frame)

        self.assertEqual(self.requests_seen(self.page(commit)), [])


class ClaimAcceptedFrozenPage(unittest.TestCase):
    """S6.1"""

    def test_hung_is_visible_is_bounded(self):
        class Hung:
            async def is_visible(self):
                await asyncio.Event().wait()

        async def nothing_sent(*args, **kwargs):
            return None, 0, False

        began = time.monotonic()
        with unittest_mock.patch.object(claim_common, "click_for_response", nothing_sent), \
                unittest_mock.patch.object(claim_common, "cooldown_message", lambda page: Hung()), \
                unittest_mock.patch.object(claim_common, "claim_button", lambda page: None), \
                unittest_mock.patch.object(claim_common, "CLAIM_BODY_TIMEOUT_SECONDS", 0.2):
            result, error, out = quietly(claim_common.claim_accepted(FakePage(), EMAIL, PASSWORD))
        self.assertIsNone(error)
        self.assertIs(result, False)
        self.assertIn("Claim failed: click sent no claim request", out)
        self.assertLess(time.monotonic() - began, 0.2 + 1)


def inputs_of_a_megabyte():
    """S7.2"""
    rng = random.Random(7)
    size = 1_000_000
    run_255 = "a" * 255
    return {
        "255-character runs joined by /": "/".join([run_255] * (size // 256 + 1)),
        "cookie repeated 40 times then /": "/".join(["cookie" * 40] * (size // 241 + 1)),
        "31-character runs joined by /": "/".join(["b" * 31] * (size // 32 + 1)),
        "base64 text": base64.b64encode(rng.randbytes(size * 3 // 4)).decode(),
    }


class SanitizerSpeed(unittest.TestCase):
    def timed(self, text):
        began = time.monotonic()
        out = claim_common.sanitize_detail(text)
        return out, time.monotonic() - began

    def test_unbroken_runs(self):
        """S7.1"""
        one, one_time = self.timed("a" * 1_000_000)
        four, four_time = self.timed("a" * 4_000_000)
        self.assertLess(one_time, 0.5)
        self.assertLess(four_time, 0.5)
        self.assertLessEqual(four_time, max(one_time, 0.001) * 8)
        self.assertEqual(one, "<redacted>")
        self.assertEqual(four, "<redacted>")

    def test_each_split_input_is_under_a_second(self):
        """S7.2"""
        for name, text in inputs_of_a_megabyte().items():
            with self.subTest(name):
                self.assertGreaterEqual(len(text), 1_000_000)
                _, seconds = self.timed(text)
                self.assertLess(seconds, 1)

    def test_same_inputs_through_sanitize_path(self):
        for name, text in inputs_of_a_megabyte().items():
            with self.subTest(name):
                began = time.monotonic()
                claim_common.sanitize_path(text)
                self.assertLess(time.monotonic() - began, 1)

    def test_heartbeat_is_never_held_up(self):
        """S7.4: a real task on the loop, measuring its own lateness"""
        async def heartbeat(gaps, stop):
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        async def main(text):
            gaps, stop = [], asyncio.Event()
            beat = asyncio.ensure_future(heartbeat(gaps, stop))
            await asyncio.sleep(0.05)
            for _ in range(17):     # one failed claim sanitizes up to 17 lines in a row
                claim_common.sanitize_detail(text)
                await asyncio.sleep(0)
            await asyncio.sleep(0.05)
            stop.set()
            await beat
            return gaps

        for name, text in inputs_of_a_megabyte().items():
            with self.subTest(name):
                gaps = asyncio.run(main(text))
                self.assertGreater(len(gaps), 3)
                self.assertLessEqual(max(gaps), 1)

    def test_heartbeat_control_detects_a_slow_call(self):
        """The heartbeat has to be able to see a stall, or its pass means nothing"""
        async def heartbeat(gaps, stop):
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        async def main():
            gaps, stop = [], asyncio.Event()
            beat = asyncio.ensure_future(heartbeat(gaps, stop))
            await asyncio.sleep(0.05)
            time.sleep(0.3)
            await asyncio.sleep(0.05)
            stop.set()
            await beat
            return gaps

        self.assertGreaterEqual(max(asyncio.run(main())), 0.3)


class SanitizerCap(unittest.TestCase):
    """S7.3"""

    TOKEN = "".join(random.Random(3).choice(string.ascii_letters + string.digits) for _ in range(64))

    def assert_no_fragment(self, out, secret, size=6):
        for start in range(len(secret) - size + 1):
            self.assertNotIn(secret[start:start + size], out)

    def test_cap_value(self):
        self.assertEqual(claim_common.MAX_SANITIZE_INPUT, 8192)

    def test_short_input_is_untouched_by_the_cap(self):
        self.assertEqual(claim_common.cap_input("short"), "short")
        text = "x " * (claim_common.MAX_SANITIZE_INPUT // 2)
        self.assertEqual(claim_common.cap_input(text), text)

    def test_long_input_is_cut_to_the_cap(self):
        """The patterns alone now meet S7.2, so the speed tests can't show this cut happens"""
        for text in ("word " * 10000, "a" * 100000, "/".join(["b" * 31] * 1000)):
            with self.subTest(text[:8]):
                capped = claim_common.cap_input(text)
                self.assertLessEqual(len(capped), claim_common.MAX_SANITIZE_INPUT + len("<redacted>"))
                self.assertTrue(capped.endswith("<redacted>"))

    def test_token_crossing_the_cap_at_every_offset(self):
        cap = claim_common.MAX_SANITIZE_INPUT
        for inside in range(1, len(self.TOKEN)):
            with self.subTest(inside=inside):
                text = "y " * ((cap - inside) // 2) + " " * ((cap - inside) % 2) + self.TOKEN + " end"
                out = claim_common.sanitize_detail(text, limit=20000)
                self.assert_no_fragment(out, self.TOKEN)

    def test_earlier_text_shrinking_brings_the_cut_within_the_limit(self):
        """Long runs collapse to <redacted>, so what sat at 8192 lands inside 200"""
        cap = claim_common.MAX_SANITIZE_INPUT
        prefix = ("a" * 1000 + " ") * 8
        for inside in range(1, len(self.TOKEN), 3):
            with self.subTest(inside=inside):
                pad = cap - inside - len(prefix)
                text = prefix + "n " * (pad // 2) + " " * (pad % 2) + self.TOKEN + " tail"
                out = claim_common.sanitize_detail(text, limit=100000)
                self.assert_no_fragment(out, self.TOKEN)

    def test_shrunk_output_within_the_default_limit_shows_no_fragment(self):
        cap = claim_common.MAX_SANITIZE_INPUT
        for inside in range(1, len(self.TOKEN), 3):
            with self.subTest(inside=inside):
                prefix = ("a" * 2000 + " ") * 3
                filler = "b" * (cap - inside - len(prefix) - 1)
                text = prefix + filler + " " + self.TOKEN + " tail"
                out = claim_common.sanitize_detail(text)
                self.assertLessEqual(len(out), 200)
                self.assert_no_fragment(out, self.TOKEN)

    def test_password_crossing_the_cap(self):
        cap = claim_common.MAX_SANITIZE_INPUT
        for inside in range(1, len(PASSWORD)):
            with self.subTest(inside=inside):
                text = "y " * ((cap - inside) // 2) + " " * ((cap - inside) % 2) + PASSWORD + " end"
                out = claim_common.sanitize_detail(text, secrets=(PASSWORD,), limit=20000)
                self.assertNotIn(PASSWORD, out)
                self.assert_no_fragment(out, PASSWORD, size=5)

    def test_email_crossing_the_cap(self):
        cap = claim_common.MAX_SANITIZE_INPUT
        email = "someone.long@example.test"
        for inside in range(1, len(email)):
            with self.subTest(inside=inside):
                text = "y " * ((cap - inside) // 2) + " " * ((cap - inside) % 2) + email + " end"
                out = claim_common.sanitize_detail(text, limit=20000)
                self.assert_no_fragment(out, email, size=5)

    def test_uncapped_behaviour_is_unchanged_below_the_cap(self):
        self.assertEqual(claim_common.sanitize_detail("plain words only"), "plain words only")


class SanitizePath(unittest.TestCase):
    """S5.8, at unit level"""

    def test_paths_of_the_site_stay_readable(self):
        for target in ("www.gameofwar-fireage.com/api/appcharge/collect-offer",
                       "www.gameofwar-fireage.com/api/tunnel/analytics/post-event",
                       "www.gameofwar-fireage.com/api/appcharge/get-item-by-sku/webportalfreedaily",
                       "/api/auth/callback/credentials", "127.0.0.1/api/account/logout"):
            with self.subTest(target):
                self.assertEqual(claim_common.sanitize_path(target), target)

    def test_a_token_segment_is_replaced_and_the_rest_kept(self):
        token = "A1b2C3d4" * 5
        self.assertEqual(claim_common.sanitize_path(f"app.test/api/{token}/data"), "app.test/api/<redacted>/data")

    def test_password_with_a_slash_or_encoded(self):
        self.assertEqual(claim_common.sanitize_path("h/api/pa/ss/x", secrets=("pa/ss",)), "h/api/<redacted>/x")
        self.assertEqual(claim_common.sanitize_path("h/api/p%21ss/x", secrets=("p!ss",)), "h/api/<redacted>/x")

    def test_email_segment(self):
        self.assertEqual(claim_common.sanitize_path("h/api/user@x.test/y"), "h/api/<email>/y")

    def test_limit(self):
        self.assertLessEqual(len(claim_common.sanitize_path("/".join(["segment"] * 100))), 200)

    def test_non_string_does_not_raise(self):
        self.assertEqual(claim_common.sanitize_path(None), "<unprintable>")


class EmailFieldS8(unittest.TestCase):
    """S8.1"""

    def test_accepted(self):
        for email in ("a@x.test", "a@b@c.test", "nouser"):
            with self.subTest(email):
                self.assertTrue(claim_common.is_valid_email_field(email))

    def test_rejected(self):
        for email in ("user@x.test#hunter", "a@x.test)q", "a@"):
            with self.subTest(email):
                self.assertFalse(claim_common.is_valid_email_field(email))


if __name__ == "__main__":
    unittest.main()
