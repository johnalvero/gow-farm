"""Control experiments against the mock, plus the page helpers on real Chromium.

The controls do the wrong thing on purpose. Each shows the mock would have
caught the naive approach, which is what makes the end-to-end passes mean anything.
Only ever navigates to the local mock or to set_content pages.
"""

import asyncio
import contextlib
import io
import os
import signal
import time
import unittest
from unittest import mock as unittest_mock

import gow_support  # noqa: F401 - pins the base URL before claim_common is imported
from gow_support import PASSWORD
from mock_site import Account, Mock
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

import claim_common

EMAIL = "control@x.test"


class BrowserCase(unittest.IsolatedAsyncioTestCase):
    def make_mock(self, *args, **kwargs):
        mock = Mock(*args, **kwargs)
        self.addCleanup(mock.close)
        return mock

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def settle(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            await asyncio.sleep(0.05)


class MockDiscriminates(BrowserCase):
    async def test_load_never_fires(self):
        mock = self.make_mock()
        for url in (f"{mock.base_url}/login", f"{mock.base_url}/catalog/main"):
            with self.subTest(url):
                with self.assertRaises(PlaywrightTimeoutError):
                    await self.page.goto(url, wait_until="load", timeout=2000)

    async def test_network_never_goes_idle(self):
        mock = self.make_mock()
        for url in (f"{mock.base_url}/login", f"{mock.base_url}/catalog/main"):
            with self.subTest(url):
                with self.assertRaises(PlaywrightTimeoutError):
                    await self.page.goto(url, wait_until="networkidle", timeout=2000)

    async def test_filling_on_visibility_loses_the_credentials(self):
        mock = self.make_mock(hydrate_ms=2000)
        await self.page.goto(f"{mock.base_url}/login", wait_until="domcontentloaded")
        await self.page.locator("input").first.wait_for(state="visible")
        await self.page.locator("input").first.fill(EMAIL)
        await self.page.locator("input").nth(1).fill(PASSWORD)

        login = self.page.locator('button:has-text("Login")')
        await claim_common.wait_interactive(login, 5000)
        await login.click()
        await self.settle(lambda: mock.logins)

        self.assertEqual(mock.logins, [("", "")])

    async def test_login_click_before_hydration_is_ignored(self):
        mock = self.make_mock(hydrate_ms=1500)
        await self.page.goto(f"{mock.base_url}/login", wait_until="domcontentloaded")
        await self.page.locator('button:has-text("Login")').click()
        await asyncio.sleep(2)
        self.assertEqual(mock.logins, [])

    async def test_navigating_right_after_a_card_click_loses_the_selection(self):
        mock = self.make_mock({EMAIL: Account(characters=("c1", "c2"), select_ms=2000)})
        await self.page.goto(f"{mock.base_url}/login", wait_until="domcontentloaded")
        login = self.page.locator('button:has-text("Login")')
        await claim_common.wait_interactive(login, 5000)
        await self.page.locator("input").first.fill(EMAIL)
        await self.page.locator("input").nth(1).fill(PASSWORD)
        await login.click()

        card = self.page.locator('[role="dialog"] button').nth(1)
        await card.wait_for(state="visible")
        await card.click()
        await asyncio.sleep(0.2)
        await self.page.goto(f"{mock.base_url}/catalog/main", wait_until="domcontentloaded")
        await self.settle(lambda: mock.cancelled_selects, timeout=4)

        self.assertEqual(mock.cancelled_selects, 1)
        self.assertEqual([s["selected"] for s in mock.sessions.values()], ["c1"])

    async def test_aborting_images_drops_load_gated_elements(self):
        mock = self.make_mock()

        async def abort_images(route):
            if route.request.resource_type == "image":
                await route.abort()
            else:
                await route.continue_()

        await self.context.route("**/*", abort_images)
        await self.page.goto(f"{mock.base_url}/gated", wait_until="domcontentloaded")
        await self.page.locator('[role="dialog"] button').wait_for(state="visible")
        await asyncio.sleep(0.3)

        self.assertEqual(await self.page.locator('#hdr button:has(img[src*="chat_avatar"])').count(), 0)
        self.assertNotIn("Total Power", await self.page.locator('[role="dialog"] button').inner_html())

    async def test_unblocked_pages_do_hit_the_image_and_analytics_gauges(self):
        mock = self.make_mock()
        await self.page.goto(f"{mock.base_url}/gated", wait_until="domcontentloaded")
        await self.settle(lambda: mock.image_hits and mock.analytics_hits)
        self.assertGreater(mock.image_hits, 0)
        self.assertGreater(mock.analytics_hits, 0)


class BlockingOnRealPages(BrowserCase):
    async def test_load_gated_elements_survive_blocking(self):
        mock = self.make_mock()
        await self.context.route("**/*", claim_common.block_nonessential)
        await self.page.goto(f"{mock.base_url}/gated", wait_until="domcontentloaded")

        avatar = self.page.locator('button:has(img[src*="chat_avatar"])').locator("visible=true").first
        await avatar.wait_for(state="visible", timeout=5000)
        card = self.page.locator('[role="dialog"] button:has(img[alt="Total Power"])')
        await card.wait_for(state="visible", timeout=5000)

        self.assertIn("Total Power", await card.inner_html())
        self.assertEqual(mock.image_hits, 0)
        self.assertEqual(mock.analytics_hits, 0)

    async def test_fonts_stop_at_the_route(self):
        mock = self.make_mock()
        await self.context.route("**/*", claim_common.block_nonessential)
        await self.page.goto(f"{mock.base_url}/login", wait_until="domcontentloaded")
        await self.page.locator("input").first.wait_for(state="visible")
        await asyncio.sleep(0.5)
        self.assertEqual(mock.font_hits, 0)


class WaitInteractive(BrowserCase):
    PAGE = "<input id=a><script>setTimeout(()=>{%s}, 300)</script>"

    async def test_returns_after_hydration(self):
        await self.page.set_content(self.PAGE % "a.__reactProps$x = {}")
        await claim_common.wait_interactive(self.page.locator("#a"), 3000)
        self.assertTrue(await self.page.locator("#a").evaluate(claim_common.IS_HYDRATED_JS))

    async def test_survives_the_node_being_replaced_by_hydration(self):
        await self.page.set_content(self.PAGE % "const n = a.cloneNode(); n.__reactProps$x = {}; a.replaceWith(n)")
        await claim_common.wait_interactive(self.page.locator("#a"), 3000)

    async def test_waits_for_a_node_not_yet_in_the_dom(self):
        await self.page.set_content(
            "<script>setTimeout(()=>{const n=document.createElement('input');n.id='a';"
            "n.__reactProps$x={};document.body.appendChild(n)},300)</script>")
        await claim_common.wait_interactive(self.page.locator("#a"), 3000)

    async def test_times_out_loudly_when_never_hydrated(self):
        await self.page.set_content("<input id=a>")
        began = time.monotonic()
        with self.assertRaises(PlaywrightTimeoutError):
            await claim_common.wait_interactive(self.page.locator("#a"), 500)
        self.assertLess(time.monotonic() - began, 3)

    async def test_raises_when_the_page_is_closed(self):
        await self.page.set_content("<input id=a>")
        await self.page.close()
        with self.assertRaises(claim_common.PlaywrightError):
            await claim_common.wait_interactive(self.page.locator("#a"), 5000)


class RealDriverStop(unittest.IsolatedAsyncioTestCase):
    """kill_driver uses private Playwright attributes, so check them on the real driver:
    if an upgrade moved them, the AttributeError it swallows would hide that"""

    async def asyncSetUp(self):
        # These tests kill the driver on purpose, and run_accounts isn't here to
        # install safe_exception_handler over the report that produces
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: None)

    async def start(self):
        manager = async_playwright()
        await manager.__aenter__()
        return manager, manager._connection._transport._proc

    async def test_private_path_reaches_the_real_process(self):
        manager, proc = await self.start()
        self.assertIsNone(proc.returncode)
        claim_common.kill_driver(manager)
        self.assertIsNotNone(await asyncio.wait_for(proc.wait(), 5))
        with contextlib.redirect_stdout(io.StringIO()):
            await claim_common.stop_playwright(manager)

    async def test_clean_stop_leaves_no_driver_and_no_warning(self):
        manager, proc = await self.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await claim_common.stop_playwright(manager)
        self.assertIsNotNone(await asyncio.wait_for(proc.wait(), 5))
        self.assertEqual(out.getvalue(), "")

    async def test_frozen_driver_is_bounded_and_killed(self):
        manager, proc = await self.start()
        os.kill(proc.pid, signal.SIGSTOP)
        out = io.StringIO()
        began = time.monotonic()
        with unittest_mock.patch.object(claim_common, "PLAYWRIGHT_STOP_TIMEOUT_SECONDS", 0.5), \
                contextlib.redirect_stdout(out):
            await claim_common.stop_playwright(manager)
        self.assertLess(time.monotonic() - began, 3)
        self.assertIn("[WARNING] Error stopping Playwright: TimeoutError", out.getvalue())
        self.assertIsNotNone(await asyncio.wait_for(proc.wait(), 5))


class FillErrors(BrowserCase):
    """The boundary the password rests on: what Playwright actually puts in the message"""

    async def test_first_line_of_real_fill_errors_omits_the_value(self):
        for html in ("<input disabled>", "<input readonly>", "<input type=number>",
                     "<input type=checkbox>", "<input type=date>", "<input type=color>", "<div>none</div>"):
            with self.subTest(html):
                await self.page.set_content(html)
                with self.assertRaises(claim_common.PlaywrightError) as caught:
                    await self.page.locator("input").first.fill(PASSWORD, timeout=500)
                self.assertNotIn(PASSWORD, claim_common.first_line(caught.exception))


if __name__ == "__main__":
    unittest.main()
