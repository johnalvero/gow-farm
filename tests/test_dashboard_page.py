"""Dashboard D8: the page in a real browser, on loopback only. Text is read from Python:
the page's CSP correctly blocks wait_for_function with a string."""

import fcntl
import json
import os
import re
import time
import unittest
from datetime import datetime, timedelta, timezone

from dash_support import Attacker, DashTestCase, Stub, account_end, event, run_start, wait_online, write_events
from gow_support import PASSWORD, PROJECT, Account, wait_until
from playwright.sync_api import sync_playwright

from dashboard import events

FAST = {"default_stagger": 0.1}
EMAIL = "page@x.test"
SLOW = Account(vault_delay_ms=25000)
STATIC = os.path.join(PROJECT, "dashboard", "static")


def static(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


class PageCase(DashTestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def page(self, dash, **options):
        context = self.browser.new_context(**options)
        self.addCleanup(context.close)
        page = context.new_page()
        page.seen = {"requests": [], "console": [], "errors": []}
        page.on("request", lambda r: page.seen["requests"].append(r.url))
        page.on("console", lambda m: page.seen["console"].append((m.type, m.text)))
        page.on("pageerror", lambda e: page.seen["errors"].append(str(e)))
        page.goto(f"http://127.0.0.1:{dash.port}/")
        return page

    def wait_text(self, page, selector, text, timeout=5):
        wait_until(lambda: text in page.inner_text(selector), timeout, f"{text!r} in {selector}")

    def wait_primary(self, page, text, timeout=8):
        self.wait_text(page, "#primary", text, timeout)

    def slot_cards(self, page):
        return [card.inner_text() for card in page.query_selector_all("#slots .slot")]

    def slot_stages(self, page):
        return [stage.inner_text() for stage in page.query_selector_all("#slots .slot-stage")]


class Empty(PageCase):
    def test_ui3_every_empty_state(self):
        dash = self.dash()
        page = self.page(dash)
        self.wait_text(page, "#status", "Idle")
        wait_until(lambda: page.query_selector_all("#chart .day"), 5, "the chart")
        for name in ("jewels", "done", "already", "failed", "left", "rate"):
            self.assertEqual(page.inner_text(f"#kpi-{name}"), "–")
            self.assertEqual(page.inner_text(f"#kpi-{name}-note"), "No runs yet")
        self.assertEqual(page.inner_text("#progress-text"), "No run yet")
        self.assertEqual(len(page.query_selector_all("#chart .day")), 14)
        self.assertTrue(all("Idle" in card for card in self.slot_cards(page)))
        for selector, text in (("#chart-empty", "No jewels recorded yet"), ("#results-empty", "No results yet"),
                               ("#reasons-empty", "No failures in this run."), ("#rows-empty", "No accounts yet"),
                               ("#log-empty", "Nothing yet"), ("#runs-empty", "No runs yet"), ("#retry", "Nothing to retry")):
            with self.subTest(selector):
                self.assertTrue(page.is_visible(selector))
                self.assertEqual(page.inner_text(selector), text)
        self.assertTrue(page.is_disabled("#retry"))
        body = page.inner_text("body")
        for word in ("NaN", "undefined", "null", "Sample", "Pause"):
            self.assertNotIn(word, body)

    def test_ui2_panels_and_colours(self):
        dash = self.dash()
        page = self.page(dash)
        self.wait_text(page, "#status", "Idle")
        for label in ("Browsers running at once", "Run totals", "Run progress", "Jewels by day", "Results",
                      "Accounts", "Accounts processed, newest first", "Live log", "Recent runs"):
            with self.subTest(label):
                self.assertTrue(page.query_selector(f'[aria-label="{label}"]'))
        css = static("app.css").lower()
        for colour in ("#0f1418", "#171e24", "#202932", "#2c3741", "#e9eef1", "#a3b3be", "#3dd6c0", "#f2b84b",
                       "#ff9478", "#35414b", "#4f6b78"):
            with self.subTest(colour):
                self.assertIn(colour, css)

    def test_ui6_controls(self):
        dash = self.dash({EMAIL: SLOW}, harness={}, child_harness=FAST)
        page = self.page(dash)
        self.wait_text(page, "#status", "Idle")
        self.assertEqual([b.inner_text() for b in page.query_selector_all("[data-concurrency]")], ["1", "2", "3", "5"])
        self.assertNotIn("pause", page.content().lower())
        page.click('[data-concurrency="2"]')
        self.assertEqual(page.get_attribute('[data-concurrency="2"]', "aria-pressed"), "true")
        wait_online(page)
        page.click("#primary")
        wait_until(lambda: dash.spawns(), 10, "the spawn")
        self.assertEqual(dash.spawns()[0]["argv"][-2:], ["--concurrency", "2"])

    def test_ui6_retry_shows_the_failed_count_and_retry5_disabled_when_none(self):
        accounts = {"good@x.test": Account(), "bad@x.test": Account(password="other"), "worse@x.test": Account(password="other")}
        dash = self.dash(accounts, child_harness=FAST)
        page = self.page(dash)
        self.assertEqual(page.inner_text("#retry"), "Nothing to retry")
        self.assertTrue(page.is_disabled("#retry"))
        wait_online(page)
        page.click("#primary")
        self.wait_text(page, "#retry", "Retry 2 failed", 40)
        self.assertTrue(page.is_enabled("#retry"))

    def test_ui9_narrow_and_reduced_motion(self):
        run = "20261101T000000Z-1"
        dash = self.dash()
        write_events(dash.events_file, [run_start(run, 1000.0), account_end(run, 1001.0, 1),
                                        event("run_end", run, 1002.0, status="finished", dropped=0)])
        page = self.page(dash, viewport={"width": 390, "height": 800}, reduced_motion="reduce")
        self.wait_text(page, "#status", "Finished")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('.fill')).transitionDuration"), "0s")
        self.assertTrue(page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))

    def test_ui9_motion_is_on_otherwise(self):
        """A control for the line above: the rule has something to switch off"""
        dash = self.dash()
        page = self.page(dash)
        self.wait_text(page, "#status", "Idle")
        self.assertNotEqual(page.evaluate("getComputedStyle(document.querySelector('.fill')).transitionDuration"), "0s")


class LiveRun(PageCase):
    def test_ui1_own_origin_only_and_no_csp_or_console_errors(self):
        dash = self.dash({"quick@x.test": Account(), EMAIL: SLOW}, child_harness=FAST)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["totals"] and s["totals"]["done"] == 1, message="a result")
        page = self.page(dash)
        self.wait_text(page, "#status", "Running")
        time.sleep(1.5)
        origin = f"http://127.0.0.1:{dash.port}/"
        self.assertEqual([url for url in page.seen["requests"] if not url.startswith(origin)], [])
        self.assertEqual(page.seen["errors"], [])
        self.assertEqual([m for m in page.seen["console"] if m[0] in ("error", "warning")], [])
        self.assertEqual([url for url in page.seen["requests"] if dash.token in url], [])
        for name in ("index.html", "app.js", "app.css"):
            with self.subTest(name):
                self.assertNotRegex(static(name), r"https?://")

    def test_ui8_no_innerhtml(self):
        self.assertNotIn("innerHTML", static("app.js"))

    def test_ui5_browser_cards(self):
        accounts = {"aa@x.test": SLOW, "bb@x.test": SLOW}
        dash = self.dash(accounts, child_harness=FAST)
        page = self.page(dash)
        self.assertEqual(dash.run(3), 200)
        dash.wait_state(lambda s: len(s["slots"]) == 2 and all(slot["stage"] == "vault" for slot in s["slots"]),
                        timeout=30, message="both accounts on the vault")
        wait_until(lambda: self.slot_stages(page) == ["Vault", "Vault", ""], 4, "the cards")
        cards = self.slot_cards(page)
        self.assertEqual(len(cards), 3)
        self.assertEqual(sorted(card.split()[0] for card in cards[:2]), ["aa@x.test", "bb@x.test"])
        self.assertIn("Idle", cards[2])

    def test_run2_page_button_for_a_terminal_run(self):
        dash = self.dash({EMAIL: SLOW})
        self.terminal_run(dash)
        page = self.page(dash)
        self.wait_primary(page, "Running in terminal")
        self.assertTrue(page.is_disabled("#primary"))
        self.wait_text(page, "#status", "Running")

    def test_live1_reload_mid_run_keeps_everything_and_stop_still_works(self):
        accounts = {"quick@x.test": Account(), EMAIL: SLOW}
        dash = self.dash(accounts, child_harness=FAST)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["totals"] and s["totals"]["claimed"] == 1 and s["slots"], message="the first claim")
        page = self.page(dash)
        self.wait_primary(page, "Stop")
        selectors = ("#kpi-jewels", "#kpi-done", "#progress-text", "#rows", "#primary")
        before = [page.inner_text(s) for s in selectors]
        began = time.monotonic()
        page.reload()
        self.wait_primary(page, "Stop", 2)
        self.wait_text(page, "#kpi-jewels", "75", 2)
        self.assertLessEqual(time.monotonic() - began, 2.5)
        self.assertEqual([page.inner_text(s) for s in selectors], before)
        self.assertEqual(page.inner_text("#slots .slot-name"), dash.state()["slots"][0]["label"])
        self.assertTrue(page.inner_text("#log"))
        page.click("#primary")
        self.wait_text(page, "#status", "Stopped", 40)

    def test_live2_account_end_reaches_the_page_quickly(self):
        run = "20261101T000000Z-1"
        dash = self.dash()
        lock = events.lock_path(dash.events_file, run)
        os.makedirs(os.path.dirname(lock), mode=0o700)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        write_events(dash.events_file, [run_start(run, time.time(), total=2)])
        page = self.page(dash)
        self.wait_text(page, "#status", "Running")
        write_events(dash.events_file, [account_end(run, time.time(), 1)])
        began = time.monotonic()
        self.wait_text(page, "#kpi-jewels", "75", 2.5)
        self.assertLessEqual(time.monotonic() - began, 2.5)

    def test_live4_page_shows_crashed_and_start_comes_back(self):
        dash = self.dash({EMAIL: SLOW}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["slots"], message="the account")
        page = self.page(dash)
        self.wait_primary(page, "Stop")
        os.kill(dash.child_pid(), 9)
        self.wait_text(page, "#status", "Crashed", 2.5)
        self.wait_primary(page, "Start run", 3)
        self.assertTrue(page.is_enabled("#primary"))
        self.assertIn("Unfinished", page.inner_text("#rows"))

    def test_live3_page_after_a_server_restart(self):
        dash = self.dash({EMAIL: Account(vault_delay_ms=10000)}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["live"], message="the run")
        dash.kill_server()
        dash.start()
        page = self.page(dash)
        self.wait_primary(page, "Stop unavailable")
        self.assertTrue(page.is_disabled("#primary"))
        self.wait_text(page, "#status", "Finished", 40)

    def test_live8_quiet_but_alive_warns_rather_than_crashing(self):
        run = "20261101T000000Z-1"
        dash = self.dash()
        lock = events.lock_path(dash.events_file, run)
        os.makedirs(os.path.dirname(lock), mode=0o700)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        write_events(dash.events_file, [run_start(run, time.time() - 300)])
        page = self.page(dash)
        self.wait_text(page, "#alert", "No activity for")
        self.assertIn("still alive", page.inner_text("#alert"))
        self.assertEqual(page.inner_text("#status"), "Running")

    def test_live9_not_responding_then_recovered(self):
        dash = self.dash({"bb@x.test": Account(vault="cooldown")}, child_harness=FAST)
        page = self.page(dash)
        self.wait_text(page, "#status", "Idle")
        port = dash.port
        dash.kill_server()
        self.wait_text(page, "#alert", "Dashboard not responding", 4)
        dash.requested_port = port
        dash.start()
        self.assertEqual(dash.port, port)
        wait_until(lambda: not page.is_visible("#alert"), 5, "the alert to clear")
        self.assertEqual(page.inner_text("#status"), "Idle")


class Restart(PageCase):
    NOTICE = "The dashboard restarted. Your last action was not repeated."

    def restart(self, dash):
        port = dash.port
        dash.kill_server()
        dash.requested_port = port
        dash.start()

    def page_token(self, page):
        return page.get_attribute('meta[name="gow-token"]', "content")

    def wait_token(self, page, token):
        wait_until(lambda: self.page_token(page) == token, 8, "the page to hold the new token")

    def online_page(self, dash):
        page = self.page(dash)
        wait_online(page)
        return page

    def test_a_restart_recovers_with_no_manual_reload(self):
        dash = self.dash({"bb@x.test": Account(vault="cooldown")}, harness={}, child_harness=FAST)
        page = self.online_page(dash)
        old_token = dash.token
        self.restart(dash)
        self.assertNotEqual(dash.token, old_token)
        self.wait_token(page, dash.token)
        wait_online(page)
        self.assertFalse(page.is_visible("#alert"))
        page.click("#primary")
        wait_until(lambda: dash.spawns(), 5, "the spawn with no manual reload")

    def test_concurrency_survives_the_reload(self):
        dash = self.dash()
        page = self.online_page(dash)
        page.click('[data-concurrency="5"]')
        self.restart(dash)
        self.wait_token(page, dash.token)
        wait_online(page)
        self.assertEqual(page.get_attribute('[data-concurrency="5"]', "aria-pressed"), "true")

    def test_live9_a_page_from_before_a_restart_is_told_why_start_failed(self):
        """A frozen poll can't see the new server, so the click goes out with the old token.
        Once polls resume, the page reloads itself, says why, and repeats nothing."""
        dash = self.dash({"bb@x.test": Account(vault="cooldown")}, harness={}, child_harness=FAST)
        page = self.online_page(dash)
        held, frozen = [], [True]
        page.route("**/api/state", lambda route: held.append(route) if frozen else route.continue_())
        page.wait_for_timeout(1500)
        self.assertTrue(held)
        self.restart(dash)
        page.click("#primary")
        self.wait_text(page, "#alert", "Token not accepted")
        self.assertEqual(dash.spawns(), [])

        frozen.clear()
        for route in held:
            route.continue_()
        self.wait_text(page, "#alert", self.NOTICE, 8)
        self.assertEqual(self.page_token(page), dash.token)
        self.assertEqual(dash.spawns(), [])

    def test_an_open_warning_gets_the_restart_message(self):
        stub = Stub(self, "200")
        dash = self.dash({"bb@x.test": Account(vault="cooldown")}, harness={}, stub=stub)
        page = self.page(dash)
        self.wait_text(page, "#network", "No internet")
        page.click("#primary")
        self.assertTrue(page.is_visible("#gate"))
        self.restart(dash)
        self.wait_text(page, "#alert", self.NOTICE, 8)
        self.assertFalse(page.is_visible("#gate"))
        self.assertEqual(dash.spawns(), [])

    def test_a_post_held_across_a_restart_is_not_repeated(self):
        dash = self.dash({"bb@x.test": Account(vault="cooldown")}, harness={}, child_harness=FAST)
        page = self.online_page(dash)
        posts, held = [], []

        def hold(route):
            posts.append(route.request.url)
            held.append(route)

        page.route("**/api/run", hold)
        page.click("#primary")
        wait_until(lambda: page.evaluate("1") and held, 3, "the held POST")
        old_token = self.page_token(page)
        self.restart(dash)
        page.wait_for_timeout(2500)
        self.assertEqual(self.page_token(page), old_token, "a reload must wait for the POST in flight")

        held[0].continue_()
        self.wait_token(page, dash.token)
        self.wait_text(page, "#alert", self.NOTICE, 8)
        page.wait_for_timeout(1500)
        self.assertEqual(len(posts), 1)
        self.assertEqual(dash.spawns(), [])
        self.assertEqual(len([url for url in page.seen["requests"] if url.endswith("/api/run")]), 1)

    def test_the_token_never_reaches_session_storage(self):
        dash = self.dash()
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.add_init_script("""
            const realSet = Storage.prototype.setItem;
            Storage.prototype.setItem = function (key, value) {
              if (this === sessionStorage) {
                localStorage.setItem('seen', (localStorage.getItem('seen') || '') + key + '=' + value + ';');
              }
              return realSet.call(this, key, value);
            };
        """)
        page.goto(f"http://127.0.0.1:{dash.port}/")
        wait_online(page)
        old_token = self.page_token(page)
        page.click('[data-concurrency="2"]')
        self.restart(dash)
        self.wait_token(page, dash.token)
        seen = page.evaluate("localStorage.getItem('seen')")
        self.assertIn("gow-concurrency=2", seen)
        for token in (old_token, dash.token):
            self.assertNotIn(token, seen)
        self.assertEqual(page.evaluate("sessionStorage.length"), 0)
        self.assertEqual(re.findall(r"sessionStorage\.setItem\('([^']+)'", static("app.js")),
                         ["gow-concurrency", "gow-notice"])

    def test_a_running_page_reloads_and_keeps_going(self):
        dash = self.dash({EMAIL: Account(vault_delay_ms=8000)}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["live"], message="the run")
        page = self.page(dash)
        self.wait_primary(page, "Stop")
        self.restart(dash)
        self.wait_token(page, dash.token)
        self.wait_primary(page, "Stop unavailable")
        self.wait_text(page, "#status", "Finished", 30)


class Results(PageCase):
    def run_mixed(self):
        accounts = {
            "alpha@x.test": Account(),
            "bravo@x.test": Account(vault="cooldown"),
            "charlie@x.test": Account(password="other"),
            "delta@x.test": Account(),
        }
        dash = self.dash(accounts, child_harness=FAST)
        self.assertEqual(dash.run(5), 200)
        return dash, dash.wait_state(lambda s: s["status"] == "finished", timeout=60, message="the run")

    def test_ui4_panels_agree_with_the_state_and_the_ledger(self):
        dash, state = self.run_mixed()
        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")
        totals = state["totals"]
        self.assertEqual((totals["claimed"], totals["already"], totals["failed"]), (2, 1, 1))
        self.assertEqual(totals["jewels"], 75 * len(dash.mock.ledger_pairs()))
        self.assertEqual(page.inner_text("#kpi-jewels"), "150")
        self.assertEqual(page.inner_text("#kpi-done"), "4 / 4")
        self.assertEqual(page.inner_text("#kpi-already"), "1")
        self.assertEqual(page.inner_text("#kpi-failed"), "1")
        for name, count in (("claimed", 2), ("already", 1), ("failed", 1)):
            with self.subTest(name):
                self.assertEqual(page.inner_text(f"#legend-{name}"), str(count))
                width = page.evaluate(f"document.getElementById('bar-{name}').style.width")
                self.assertAlmostEqual(float(width.rstrip("%")), count / 4 * 100)
        self.assertEqual(page.inner_text("#ring-text"), "75%")
        self.assertIn("conic-gradient", page.evaluate("document.getElementById('ring').style.background"))
        self.assertIn("Login rejected", page.inner_text("#reasons"))
        failed = [cell for cell in page.query_selector_all(".cell-result") if "Failed" in cell.inner_text()]
        self.assertEqual(failed[0].get_attribute("title"), "Login rejected")

    def test_ui4_search_and_filters(self):
        dash, state = self.run_mixed()
        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")

        def rows():
            return [row.query_selector("span").inner_text() for row in page.query_selector_all("#rows .table-row")]

        self.assertEqual(sorted(rows()), ["1", "2", "3", "4"])
        for name, expected in (("claimed", ["1", "4"]), ("already", ["2"]), ("failed", ["3"]), ("all", ["1", "2", "3", "4"])):
            with self.subTest(name):
                page.click(f'[data-filter="{name}"]')
                self.assertEqual(sorted(rows()), expected)
        page.fill("#search", "charlie@x.test")
        self.assertEqual(rows(), ["3"])
        self.assertIn("Showing 1 of 1", page.inner_text("#rows-note"))
        page.fill("#search", "2")
        self.assertEqual(rows(), ["2"])
        page.fill("#search", "zzz")
        self.assertEqual(rows(), [])
        self.assertEqual(page.inner_text("#rows-empty"), "No accounts match.")


class Fixtures(PageCase):
    def test_ui7_days_are_utc_across_a_local_midnight(self):
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=30, second=0, microsecond=0)
        if today > now:
            today -= timedelta(days=1)
        yesterday = today - timedelta(hours=1)      # same date in Manila, the day before in UTC
        old = today - timedelta(days=20)
        entries = []
        for number, (moment, claims) in enumerate(((old, 9), (yesterday, 1), (today, 2)), 1):
            run = f"{moment.strftime('%Y%m%dT%H%M%S')}Z-{number}"
            ts = moment.timestamp()
            entries += [run_start(run, ts, total=claims)]
            entries += [account_end(run, ts + 1, line) for line in range(1, claims + 1)]
            entries += [event("run_end", run, ts + 2, status="finished", dropped=0)]
        dash = self.dash()
        write_events(dash.events_file, entries)
        page = self.page(dash, timezone_id="Asia/Manila")
        wait_until(lambda: page.query_selector_all("#chart .day"), 5, "the chart")
        days = {day.get_attribute("title").split(":")[0]: day.get_attribute("title").split(": ")[1]
                for day in page.query_selector_all("#chart .day")}
        self.assertEqual(len(days), 14)
        self.assertEqual(days[today.date().isoformat()], "150 jewels")
        self.assertEqual(days[yesterday.date().isoformat()], "75 jewels")
        self.assertNotIn(old.date().isoformat(), days)
        columns = page.query_selector_all("#chart .day")
        self.assertTrue(columns[-1].query_selector(".today"))
        self.assertEqual(columns[-1].get_attribute("title").split(":")[0], today.date().isoformat())

    HOSTILE = "<img src=x onerror=alert(1)>"

    def hostile_entries(self):
        run = "20261101T000000Z-1"
        return [run_start(run, 1000.0, total=1), account_end(run, 1001.0, 1, label=self.HOSTILE),
                event("run_end", run, 1002.0, status="finished", dropped=0)]

    def test_ui8_a_hostile_label_is_dropped_by_the_reader(self):
        dash = self.dash()
        write_events(dash.events_file, self.hostile_entries())
        self.assertNotIn(b"<img", dash.get("/api/state")[2])
        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")
        self.assertNotIn("<img", page.inner_text("body"))

    def test_ui8_with_validation_bypassed_it_renders_as_text(self):
        dash = self.dash(harness={"no_validate": True})
        write_events(dash.events_file, self.hostile_entries())
        self.assertIn(b"<img", dash.get("/api/state")[2])
        page = self.page(dash)
        self.wait_text(page, "#rows", self.HOSTILE)
        self.assertEqual(page.query_selector_all("#rows img"), [])
        self.assertEqual(page.query_selector_all("#log img"), [])
        self.assertEqual(page.seen["errors"], [])


def held_lock(case, dash, run):
    """Hold a fixture run's lock from this process, so the reader sees it as live"""
    lock = events.lock_path(dash.events_file, run)
    os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    case.addCleanup(os.close, fd)
    fcntl.flock(fd, fcntl.LOCK_EX)


ATTEMPTS = """
async (target) => {
  const out = {};
  const attempt = async (name, go) => {
    try { out[name] = await go(); } catch (error) { out[name] = 'blocked'; }
  };
  await attempt('cors', async () => (await fetch(target)).text());
  await attempt('no-cors', async () => {
    const response = await fetch(target, { mode: 'no-cors' });
    return `${response.type}:${response.status}:${await response.text()}`;
  });
  await attempt('xhr', () => new Promise((done) => {
    const request = new XMLHttpRequest();
    request.open('GET', target);
    request.onload = () => done(request.responseText);
    request.onerror = () => done('blocked');
    request.send();
  }));
  await attempt('script', () => new Promise((done) => {
    const script = document.createElement('script');
    script.src = target;
    script.onload = () => done('loaded');
    script.onerror = () => done('blocked');
    document.head.append(script);
  }));
  await attempt('iframe', () => new Promise((done) => {
    const frame = document.createElement('iframe');
    frame.src = target;
    frame.onload = () => {
      try { done(`read:${frame.contentDocument.body.innerText}`); } catch (error) { done('blocked'); }
    };
    document.body.append(frame);
    setTimeout(() => done('no load'), 1500);
  }));
  return out;
}
"""


class FullEmails(PageCase):
    ADDRESS = "victim.person@example.test"

    def fixture_run(self, dash, run="20261101T000000Z-1", label=None, ts=1000.0):
        write_events(dash.events_file, [
            run_start(run, ts, total=1), account_end(run, ts + 1, 1, label=label or self.ADDRESS),
            event("run_end", run, ts + 2, status="finished", dropped=0)])

    def test_em1_a_real_run_shows_full_emails_everywhere(self):
        accounts = {"alpha.one@x.test": Account(), "bravo@x.test": SLOW}
        dash = self.dash(accounts, child_harness=FAST)
        self.assertEqual(dash.run(2), 200)
        page = self.page(dash)
        dash.wait_state(lambda s: s["totals"] and s["totals"]["done"] == 1 and len(s["slots"]) == 1, timeout=30,
                        message="one done and one in flight")
        self.wait_text(page, "#rows", "alpha.one@x.test")
        self.wait_text(page, "#slots", "bravo@x.test")
        self.wait_text(page, "#log", "alpha.one@x.test")
        recorded = dash.recorded()
        self.assertEqual({e["label"] for e in recorded if e["event"] == "account_start"}, set(accounts))
        self.assertEqual({e["label"] for e in recorded if e["event"] == "account_end"}, {"alpha.one@x.test"})
        self.assertIn("alpha.one@x.test", json.dumps(dash.state()["accounts"]))

        for needle in ("alpha.one@x.test", "alpha.o", "one@x"):
            with self.subTest(needle):
                page.fill("#search", needle)
                self.assertIn("alpha.one@x.test", page.inner_text("#rows"))
        page.fill("#search", "no-such-account")
        self.assertNotIn("alpha.one@x.test", page.inner_text("#rows"))

        # EM8, on a file holding full emails
        self.assertEqual(os.stat(dash.events_file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(dash.events_file)).st_mode & 0o777, 0o700)

    def test_em2_old_masked_events_render_every_panel(self):
        dash = self.dash()
        old = "20261101T000000Z-1"
        write_events(dash.events_file, [
            run_start(old, 1000.0, total=3), event("account_start", old, 1000.5, line=1, label="a***@x***"),
            account_end(old, 1001.0, 1, label="a***@x***"),
            account_end(old, 1002.0, 2, label="b***", claimed=0, already=1),
            account_end(old, 1003.0, 3, label="c***@y***", claimed=0, success=False),
            event("run_end", old, 1004.0, status="finished", dropped=0)])
        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")
        self.assertEqual(dash.state()["totals"]["done"], 3)
        rows = page.inner_text("#rows")
        for label in ("a***@x***", "b***", "c***@y***"):
            self.assertIn(label, rows)
        self.assertIn("a***@x***", page.inner_text("#log"))
        self.assertIn("s/claim", page.inner_text("#runs"))
        self.assertEqual(page.inner_text("#kpi-rate"), "4.0s")
        self.assertEqual(page.seen["errors"], [])
        self.assertEqual([m for m in page.seen["console"] if m[0] in ("error", "warning")], [])

    def test_em2_old_and_new_runs_in_one_file(self):
        dash = self.dash()
        self.fixture_run(dash, "20261101T000000Z-1", "a***@x***", 1000.0)
        page = self.page(dash)
        self.wait_text(page, "#rows", "a***@x***")
        self.assertEqual([row["label"] for row in dash.state()["accounts"]], ["a***@x***"])
        self.fixture_run(dash, "20261101T000100Z-2", self.ADDRESS, 2000.0)
        self.wait_text(page, "#rows", self.ADDRESS)
        state = dash.state()
        self.assertEqual(len(state["recent_runs"]), 2)
        self.assertEqual([row["label"] for row in state["accounts"]], [self.ADDRESS])

    def test_ui4_an_old_label_is_still_found_by_search(self):
        dash = self.dash()
        self.fixture_run(dash, label="c***@x***")
        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")
        page.fill("#search", "c***")
        self.assertIn("c***@x***", page.inner_text("#rows"))
        page.fill("#search", "zzz")
        self.assertNotIn("c***@x***", page.inner_text("#rows"))

    def test_em9_a_60_character_email_causes_no_side_scroll(self):
        address = "a" * 47 + "@example.test"
        self.assertEqual(len(address), 60)
        run = "20261101T000000Z-1"
        dash = self.dash()
        held_lock(self, dash, run)
        now = time.time()
        write_events(dash.events_file, [
            run_start(run, now, total=2), event("account_start", run, now, line=1, label=address),
            account_end(run, now, 2, label=address)])
        for width in (1360, 390):
            with self.subTest(width):
                page = self.page(dash, viewport={"width": width, "height": 800})
                self.wait_text(page, "#slots", address)
                self.wait_text(page, "#rows", address)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_ep2_another_origin_reads_nothing(self):
        dash = self.dash()
        self.fixture_run(dash)
        attacker = Attacker(self)
        for page_host in ("127.0.0.1", "localhost"):
            for target_host in ("127.0.0.1", "localhost"):
                with self.subTest(page_host=page_host, target_host=target_host):
                    context = self.browser.new_context()
                    self.addCleanup(context.close)
                    page = context.new_page()
                    statuses = []
                    page.on("response", lambda r: statuses.append(r.status) if "/api/state" in r.url else None)
                    page.goto(f"http://{page_host}:{attacker.port}/")
                    seen = page.evaluate(ATTEMPTS, f"http://{target_host}:{dash.port}/api/state")
                    self.assertEqual(sorted(seen), ["cors", "iframe", "no-cors", "script", "xhr"])
                    self.assertNotIn("victim", json.dumps(seen))
                    self.assertEqual((seen["cors"], seen["xhr"], seen["script"]), ("blocked", "blocked", "blocked"))
                    self.assertEqual(seen["no-cors"], "opaque:0:")
                    self.assertIn(seen["iframe"], ("blocked", "no load", "read:"))
                    self.assertTrue(statuses, "the attempts have to reach the server")
                    self.assertEqual(set(statuses), {403}, "the emails must not even be sent")

    def test_ep4_own_polling_works_at_both_hosts(self):
        dash = self.dash()
        self.fixture_run(dash)
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host):
                context = self.browser.new_context()
                self.addCleanup(context.close)
                page = context.new_page()
                failures = []
                page.on("response", lambda r: failures.append(r.status) if r.status >= 400 else None)
                page.goto(f"http://{host}:{dash.port}/")
                self.wait_text(page, "#rows", self.ADDRESS)
                self.wait_text(page, "#status", "Finished")
                page.wait_for_timeout(2500)
                self.assertFalse(page.is_visible("#alert"))
                self.assertEqual(failures, [])

    def test_ep6_an_open_page_recovers_after_a_restart(self):
        dash = self.dash()
        self.fixture_run(dash)
        page = self.page(dash)
        self.wait_text(page, "#rows", self.ADDRESS)
        port = dash.port
        dash.kill_server()
        self.wait_text(page, "#alert", "Dashboard not responding", 6)
        dash.requested_port = port
        dash.start()
        wait_until(lambda: page.get_attribute('meta[name="gow-token"]', "content") == dash.token, 8, "the new token")
        self.wait_text(page, "#rows", self.ADDRESS)
        wait_until(lambda: not page.is_visible("#alert"), 5, "the alert to clear")


class ClaimRate(PageCase):
    def finished(self, dash, claims, seconds, already=0):
        """Events as written before this change: no new field, and rate worked out on read"""
        now = time.time() - 1000
        run = "20261101T000000Z-1"
        entries = [run_start(run, now, total=claims + already)]
        entries += [account_end(run, now + 1, line) for line in range(1, claims + 1)]
        entries += [account_end(run, now + 1, claims + line, claimed=0, already=1) for line in range(1, already + 1)]
        entries += [event("run_end", run, now + seconds, status="finished", dropped=0)]
        write_events(dash.events_file, entries)

    def test_spc5_spc6_spc7_the_kpi_and_recent_runs(self):
        dash = self.dash()
        self.finished(dash, 10, 176)
        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")
        label = page.query_selector("#kpi-rate").evaluate("element => element.parentElement.querySelector('.kpi-label').textContent")
        self.assertEqual(label, "Seconds per claim")
        self.assertEqual(page.inner_text("#kpi-rate"), "17.6s")
        self.assertEqual(page.inner_text("#kpi-rate-note"), "10 claims in 2m 56s")
        self.assertEqual(" ".join(page.inner_text("#runs").split()).count("17.6 s/claim"), 1)

    def test_spc2_spc6_spc7_no_claims(self):
        dash = self.dash()
        self.finished(dash, 0, 100, already=3)
        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")
        self.assertIsNone(dash.state()["run"]["seconds_per_claim"])
        self.assertEqual(page.inner_text("#kpi-rate"), "–")
        self.assertEqual(page.inner_text("#kpi-rate-note"), "No claims yet")
        self.assertEqual(" ".join(page.inner_text("#runs").split()).count("– s/claim"), 1)
        text = page.inner_text("body")
        for word in ("Infinity", "NaN", "null", "undefined", "0.0s", "0 s/claim"):
            self.assertNotIn(word, text)

    def test_spc7_no_run(self):
        dash = self.dash()
        page = self.page(dash)
        self.wait_text(page, "#status", "Idle")
        self.assertEqual(page.inner_text("#kpi-rate"), "–")
        self.assertEqual(page.inner_text("#kpi-rate-note"), "No runs yet")

    def test_spc7_narrow(self):
        dash = self.dash()
        self.finished(dash, 10, 176)
        page = self.page(dash, viewport={"width": 390, "height": 800})
        self.wait_text(page, "#kpi-rate", "17.6s")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)

    def test_spc8_small_values_show_as_0_1(self):
        dash = self.dash()
        now = time.time() - 1000
        run = "20261101T000000Z-1"
        write_events(dash.events_file, [
            run_start(run, now, total=1), account_end(run, now + 0.01, 1, claimed=1000, characters=1000),
            event("run_end", run, now + 0.02, status="finished", dropped=0)])
        page = self.page(dash)
        self.wait_text(page, "#kpi-rate", "0.1s")
        self.assertIn("0.1 s/claim", " ".join(page.inner_text("#runs").split()))
        self.assertNotIn("0.0", page.inner_text("#kpi-rate"))

    def test_deviation_3_exact_duration_in_the_note(self):
        for claims, seconds, rate, note in ((1, 40, "40.0s", "1 claim in 40s"), (2, 3900, "1950.0s", "2 claims in 1h 5m"),
                                            (3, 59, "19.7s", "3 claims in 59s"), (2, 120, "60.0s", "2 claims in 2m 0s")):
            with self.subTest(note):
                dash = self.dash()
                self.finished(dash, claims, seconds)
                page = self.page(dash)
                self.wait_text(page, "#status", "Finished")
                self.assertEqual(page.inner_text("#kpi-rate-note"), note)
                self.assertEqual(page.inner_text("#kpi-rate"), rate)

    def test_deviation_4_the_stage_label_does_not_shrink(self):
        self.assertRegex(static("app.css"), r"\.slot-stage \{[^}]*flex: none")

    def test_deviation_5_the_readme_states_the_limits(self):
        with open(os.path.join(PROJECT, "README.md"), encoding="utf-8") as f:
            text = " ".join(f.read().split())
        for words in ("full account emails", "never holds a password", "stays masked", "any program running on this mac",
                      "email-shaped password", "keep their masked labels", "seconds per claim"):
            with self.subTest(words):
                self.assertIn(words, text.lower())

    def test_spc3_the_kpi_moves_on_a_real_run_with_no_reload(self):
        accounts = {"quick@x.test": Account(), EMAIL: SLOW}
        dash = self.dash(accounts, child_harness=FAST)
        self.assertEqual(dash.run(1), 200)
        page = self.page(dash)
        wait_until(lambda: page.inner_text("#kpi-rate").endswith("s"), 30, "the first value")
        first = page.inner_text("#kpi-rate")
        wait_until(lambda: page.inner_text("#kpi-rate") != first, 6, "the value to move")
        self.assertRegex(page.inner_text("#kpi-rate"), r"^\d+\.\ds$")

    def test_spc6_a_live_rows_time_counts_up(self):
        run = "20261101T000000Z-1"
        dash = self.dash()
        held_lock(self, dash, run)
        started = time.time() - 100
        write_events(dash.events_file, [run_start(run, started, total=3), account_end(run, started + 1, 1)])
        first = dash.state()["recent_runs"][0]
        self.assertEqual(first["claims"], 1)
        time.sleep(2.1)
        second = dash.state()["recent_runs"][0]
        self.assertGreater(second["seconds"], first["seconds"])
        self.assertGreater(second["seconds_per_claim"], first["seconds_per_claim"])
        page = self.page(dash)
        self.wait_text(page, "#runs", "s/claim")


class Secrets(PageCase):
    TOKEN = "Zx9Qw7Er5Ty3Ui1Op0As2Df4Gh6Jk8Lm0Nb3Vc5X"
    MAILBOX = "mailbox-seed@x.test"

    def accounts(self):
        text = f"bad {PASSWORD} for {self.MAILBOX} token={self.TOKEN}"
        return {
            "seedalpha@x.test": Account(),
            "seedbravo@x.test": Account(collect_body=json.dumps({"error": text, "errorMessage": text})),
            "seedcharlie@x.test": Account(claim_no_request=True, click_console=(("error", text),)),
            "seeddelta@x.test": Account(password="other"),
        }

    def secrets(self, accounts):
        return [PASSWORD, self.TOKEN, self.MAILBOX, "mailbox-seed"]

    def leaks(self, secrets, blobs):
        """(where, secret) for each secret found in each named blob"""
        return [(name, secret) for name, blob in blobs.items() for secret in secrets if secret in blob]

    def event_blobs(self, dash):
        blobs = {}
        folder = os.path.dirname(dash.events_file)
        for root, directories, names in os.walk(folder):
            for name in names + directories:
                blobs[f"name {name}"] = name
            for name in names:
                if name == "last-run.log":
                    continue        # holds full emails by design, and is never served: see SEC5
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                    blobs[name] = f.read()
        return blobs

    def run_seeded(self, child_harness):
        accounts = self.accounts()
        dash = self.dash(accounts, child_harness=child_harness)
        self.assertEqual(dash.run(5), 200)
        dash.wait_state(lambda s: s["status"] == "finished" and not s["control"]["owned"], timeout=90, message="the run")
        return dash, accounts

    def test_sec1_no_secret_reaches_files_responses_or_the_page(self):
        dash, accounts = self.run_seeded(FAST)
        blobs = self.event_blobs(dash)
        self.assertIn("events.jsonl", blobs)

        for path in ("/", "/app.js", "/app.css", "/api/state", "/nope", "/api/run", "/last-run.log"):
            status, headers, data = dash.get(path)
            blobs[f"GET {path}"] = data.decode() + str(headers)
        for path, payload in (("/api/retry", {"concurrency": 3, "x": 1}), ("/api/stop", {}), ("/nope", {}),
                              ("/api/run", {"concurrency": 9})):
            status, headers, data = dash.post(path, payload)
            blobs[f"POST {path}"] = data.decode() + str(headers)
        blobs["refused POST"] = dash.post("/api/run", {"concurrency": 3}, token="wrong")[2].decode()

        page = self.page(dash)
        self.wait_text(page, "#status", "Finished")
        blobs["page text"] = page.inner_text("body")
        blobs["page html"] = page.content()
        blobs["server output"] = dash.output()

        self.assertEqual(self.leaks(self.secrets(accounts), blobs), [])
        self.assertGreater(len(blobs), 15)

    def test_sec8_control_the_detector_sees_a_real_leak(self):
        dash, accounts = self.run_seeded({**FAST, "events_raw_field": True})
        found = self.leaks(self.secrets(accounts), self.event_blobs(dash))
        self.assertIn(("events.jsonl", PASSWORD), found)


if __name__ == "__main__":
    unittest.main()
