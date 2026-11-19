"""Unit tests that need no browser: options, routing, the runner, and the suite's own guards."""

import asyncio
import contextlib
import gc
import http.client
import io
import os
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest import mock as unittest_mock

import gow_support
from gow_support import UnsafeRun, assert_safe_env
from mock_site import Mock

import claim_common  # noqa: E402 - imported after gow_support has pinned the base URL


class SuiteGuards(unittest.TestCase):
    SAFE = {"GOW_BASE_URL": "http://127.0.0.1:1234/en-us", "GOW_USERS_FILE": "/tmp/gow/fake-accounts",
            "GOW_DASHBOARD_PORT": ""}
    OVERRIDES = ("GOW_BASE_URL", "GOW_USERS_FILE")

    def test_in_process_import_is_pinned_to_localhost(self):
        self.assertTrue(claim_common.BASE_URL.startswith("http://127.0.0.1"))

    def test_safe_env_is_accepted(self):
        assert_safe_env(self.SAFE)

    def test_refuses_when_either_override_is_unset(self):
        for name in self.OVERRIDES:
            for value in (None, ""):
                with self.subTest(name, value=value):
                    env = dict(self.SAFE)
                    if value is None:
                        del env[name]
                    else:
                        env[name] = value
                    with self.assertRaises(UnsafeRun):
                        assert_safe_env(env)

    def test_refuses_non_local_base_url(self):
        for url in ("https://www.gameofwar-fireage.com/en-us", "http://127.0.0.1.example.com/en-us",
                    "http://localhost.example.com/"):
            with self.subTest(url):
                with self.assertRaises(UnsafeRun):
                    assert_safe_env({**self.SAFE, "GOW_BASE_URL": url})

    def test_refuses_project_or_real_users_files(self):
        for path in (os.path.join(gow_support.PROJECT, "users.txt"),
                     os.path.join(gow_support.PROJECT, "users-backup.txt"),
                     os.path.join(gow_support.PROJECT, "anything.txt"),
                     "/tmp/gow/users.txt"):
            with self.subTest(path):
                with self.assertRaises(UnsafeRun):
                    assert_safe_env({**self.SAFE, "GOW_USERS_FILE": path})

    def test_start_script_refuses_before_spawning(self):
        unsafe = SimpleNamespace(base_url="https://www.gameofwar-fireage.com/en-us")
        with unittest_mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(UnsafeRun):
                gow_support.run_script("claim_batch.py", unsafe, "a@x.test:p\n", "1", "1")
        popen.assert_not_called()

    def test_harness_refuses_without_overrides(self):
        env = {k: v for k, v in os.environ.items() if k not in ("GOW_BASE_URL", "GOW_USERS_FILE")}
        done = subprocess.run([gow_support.PYTHON, "-B", gow_support.HARNESS, "claim_batch.py", "1", "1"],
                              env=env, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Refusing to run", done.stderr)

    # S1: a spawned run must name its dashboard port, and never the real one
    def test_s1_refuses_a_missing_or_real_dashboard_port(self):
        missing = {k: v for k, v in self.SAFE.items() if k != "GOW_DASHBOARD_PORT"}
        with self.assertRaises(UnsafeRun):
            assert_safe_env(missing)
        for value in ("8765", "08765", "008765"):
            with self.subTest(value):
                with self.assertRaises(UnsafeRun):
                    assert_safe_env({**self.SAFE, "GOW_DASHBOARD_PORT": value})

    def test_s1_accepts_off_and_test_ports(self):
        for value in ("", "9001", "8766", "abc"):
            with self.subTest(value):
                assert_safe_env({**self.SAFE, "GOW_DASHBOARD_PORT": value})

    def test_s1_script_env_turns_the_dashboard_off(self):
        unsafe_parent = {**os.environ, "GOW_DASHBOARD_PORT": "8765"}
        with unittest_mock.patch.dict(os.environ, unsafe_parent):
            env = gow_support.script_env(SimpleNamespace(base_url="http://127.0.0.1:1/en-us"), "/tmp/gow/fake-accounts")
        self.assertEqual(env["GOW_DASHBOARD_PORT"], "")
        assert_safe_env(env)

    def test_s1_in_process_imports_have_it_off(self):
        self.assertEqual(os.environ.get("GOW_DASHBOARD_PORT"), "")

    # S2: the harness refuses before it imports anything that could open the dashboard
    def harness_with_port(self, value):
        env = {k: v for k, v in os.environ.items() if k not in ("GOW_DASHBOARD_PORT", "GOW_TEST_HARNESS")}
        if value is not None:
            env["GOW_DASHBOARD_PORT"] = value
        return subprocess.run([gow_support.PYTHON, "-B", gow_support.HARNESS, "claim_batch.py", "1", "1"],
                              env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def test_s2_harness_refuses_without_a_dashboard_port(self):
        for value in (None, "8765", "08765"):
            with self.subTest(value):
                done = self.harness_with_port(value)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("Refusing to run: GOW_DASHBOARD_PORT", done.stderr)

    def test_s2_control_an_off_port_gets_past_the_guard(self):
        # With no GOW_TEST_HARNESS it then fails on its config, before claim_common loads
        done = self.harness_with_port("")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("Refusing to run", done.stderr)
        self.assertIn("GOW_TEST_HARNESS", done.stderr)


class MockGauges(unittest.TestCase):
    """The gauges have to be able to report a problem, or a pass means nothing"""

    def test_cross_account_cookie_is_detected(self):
        mock = Mock()
        self.addCleanup(mock.close)
        connection = http.client.HTTPConnection("127.0.0.1", mock.port)
        self.addCleanup(connection.close)

        def post_login(email, cookie=None):
            headers = {"Cookie": cookie} if cookie else {}
            connection.request("POST", "/api/account/login",
                               body='{"email": "%s", "password": "%s"}' % (email, gow_support.PASSWORD),
                               headers=headers)
            response = connection.getresponse()
            response.read()
            return dict(part.split(";")[0].split("=", 1) for part in response.headers.get_all("Set-Cookie"))

        a = post_login("a@x.test")
        post_login("b@x.test")
        connection.request("GET", "/poll", headers={"Cookie": f"sid={a['sid']}; acct_0=1; acct_1=1"})
        connection.getresponse().read()
        self.assertEqual(mock.cross_account, [("a@x.test", ["acct_1"])])

    def test_peak_counts_overlap_not_total(self):
        mock = Mock()
        self.addCleanup(mock.close)
        V = gow_support.Mock.__init__.__globals__["Visitor"]
        mock.visitors = {"a": V(0, 2), "b": V(1, 3), "c": V(3, 4), "d": V(5, 6)}
        self.assertEqual(mock.peak_concurrency(), 2)


class ParseRunOptions(unittest.TestCase):
    def rejected(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            claim_common.parse_run_options(argv)
        self.assertEqual(caught.exception.code, 1)
        return out.getvalue()

    def test_defaults(self):
        options, rest = claim_common.parse_run_options([])
        self.assertEqual((options.concurrency, options.stagger, rest), (3, 10, []))

    def test_options_anywhere_among_positionals(self):
        for argv in (["2", "4", "--concurrency", "2", "--stagger", "0.5"],
                     ["--concurrency=2", "--stagger=0.5", "2", "4"],
                     ["2", "--stagger", "0.5", "4", "--concurrency", "2"]):
            with self.subTest(argv):
                options, rest = claim_common.parse_run_options(argv)
                self.assertEqual((options.concurrency, options.stagger, rest), (2, 0.5, ["2", "4"]))

    def test_boundaries_accepted(self):
        self.assertEqual(claim_common.parse_run_options(["--concurrency", "1"])[0].concurrency, 1)
        self.assertEqual(claim_common.parse_run_options(["--stagger", "0"])[0].stagger, 0)

    def test_invalid_values_exit_1_with_error_line(self):
        for argv in (["--concurrency", "0"], ["--concurrency", "abc"], ["--concurrency", "-1"],
                     ["--concurrency", "1.5"], ["--concurrency", ""], ["--concurrency"],
                     ["--stagger", "-1"], ["--stagger", "abc"], ["--stagger", "nan"],
                     ["--stagger", "inf"], ["--stagger", "-inf"], ["--stagger", ""], ["--stagger"]):
            with self.subTest(argv):
                self.assertIn("Error:", self.rejected(argv))

    def test_abbreviations_are_rejected_not_expanded(self):
        for argv, name in ((["--conc", "2"], "--conc"), (["--conc=2"], "--conc"),
                           (["--concurrenc", "2"], "--concurrenc"), (["--stag", "5"], "--stag")):
            with self.subTest(argv):
                self.assertEqual(self.rejected(argv).strip(), f"Error: unknown option {name}")

    def test_mistyped_flags_name_the_flag_only(self):
        for argv, name in ((["--concurency", "1"], "--concurency"), (["--concurency=1"], "--concurency"),
                           (["--bogus"], "--bogus"), (["1", "2", "--bogus"], "--bogus"),
                           (["--password=SECRET"], "--password"), (["--password", "SECRET"], "--password"),
                           (["--help=SECRET"], "--help")):
            with self.subTest(argv):
                self.assertEqual(self.rejected(argv).strip(), f"Error: unknown option {name}")

    def test_help_prints_usage_and_exits_0(self):
        for argv in (["--help"], ["1", "2", "--help"], ["--stagger", "1", "--help"]):
            with self.subTest(argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
                    claim_common.parse_run_options(argv, "Usage: text")
                self.assertEqual(caught.exception.code, 0)
                self.assertEqual(out.getvalue().strip(), "Usage: text")

    def test_bad_value_beats_help(self):
        self.assertIn("Error:", self.rejected(["--concurrency", "0", "--help"]))

    def test_positional_and_single_dash_args_stay_leftovers(self):
        for argv in (["extra"], ["1", "-5"], ["-1", "4"], ["-x"], ["-password=x"]):
            with self.subTest(argv):
                self.assertEqual(claim_common.parse_run_options(argv)[1], argv)

    def test_negative_positional_stays_positional(self):
        _, rest = claim_common.parse_run_options(["-1", "4"])
        self.assertEqual(rest, ["-1", "4"])


class NewFlags(unittest.TestCase):
    def parse(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            options, rest = claim_common.parse_run_options(argv)
        return options, rest, out.getvalue()

    def test_defaults_are_off(self):
        options, _, out = self.parse([])
        self.assertFalse(options.no_block)
        self.assertFalse(options.force_concurrency)
        self.assertIs(options.no_dashboard, False)
        self.assertEqual(out, "")

    # A1
    def test_no_dashboard_is_set_quietly(self):
        for argv in (["--no-dashboard"], ["1", "2", "--no-dashboard"], ["--no-dashboard", "--stagger", "0"]):
            with self.subTest(argv):
                options, rest, out = self.parse(argv)
                self.assertIs(options.no_dashboard, True)
                self.assertEqual(out, "")
                self.assertNotIn("--no-dashboard", rest)

    def test_no_dashboard_takes_no_value(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            claim_common.parse_run_options(["--no-dashboard=SECRET"])
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(out.getvalue().startswith("Error:"), out.getvalue())

    def test_no_block_warns(self):
        options, _, out = self.parse(["--no-block"])
        self.assertTrue(options.no_block)
        self.assertRegex(out, r"\[WARNING\] Resource blocking disabled \(--no-block\)")

    def test_ceiling_is_5(self):
        self.assertEqual(claim_common.MAX_CONCURRENCY, 5)
        options, _, out = self.parse(["--concurrency", "5"])
        self.assertEqual(options.concurrency, 5)
        self.assertEqual(out, "")

    def test_above_the_ceiling_needs_the_flag(self):
        for value in ("6", "10", "1000"):
            with self.subTest(value):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
                    claim_common.parse_run_options(["--concurrency", value])
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(out.getvalue().strip(), "Error: --concurrency above 5 needs --force-concurrency")

    def test_force_names_the_value(self):
        options, _, out = self.parse(["--concurrency", "6", "--force-concurrency"])
        self.assertEqual(options.concurrency, 6)
        self.assertRegex(out, r"\[WARNING\] .*concurrency 6")

    def test_force_alone_changes_nothing(self):
        options, _, out = self.parse(["--force-concurrency"])
        self.assertEqual(options.concurrency, 3)
        self.assertEqual(out, "")

    def test_mistyped_new_flags_are_rejected(self):
        for flag in ("--no-blok", "--force-concurency", "--no", "--no-dash", "--no-dashbord", "--no-dashboards"):
            with self.subTest(flag):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
                    claim_common.parse_run_options([flag])
                self.assertEqual(caught.exception.code, 1)
                self.assertIn(f"Error: unknown option {flag}", out.getvalue())


class EmailField(unittest.TestCase):
    def test_fields_that_could_hold_a_password_are_rejected(self):
        for email in ("user@x.test hunter", "user@x.test\thunter", "user@x.test,hunter", "user@x.test;hunter",
                      "a b", "user@x.test\u00a0hunter", "user@x.test\nhunter"):
            with self.subTest(email):
                self.assertFalse(claim_common.is_valid_email_field(email))

    def test_domain_after_the_last_at_must_look_like_a_domain(self):
        for email in ("user@x.test|hunter", "user@x.test/hunter", "user@x.test!hunter", "user@"):
            with self.subTest(email):
                self.assertFalse(claim_common.is_valid_email_field(email))

    def test_ordinary_fields_are_accepted(self):
        for email in ("user@x.test", "first.last+tag@sub.x.test", "username-without-at", "ünï@x.test"):
            with self.subTest(email):
                self.assertTrue(claim_common.is_valid_email_field(email))


class SanitizeDetail(unittest.TestCase):
    TOKEN = "Ab3" * 22

    def test_order_of_the_redactions(self):
        text = f"hunter2 mail a.b+c@x.test token=abc123 {self.TOKEN}\nsecond line hunter2"
        self.assertEqual(claim_common.sanitize_detail(text, secrets=("hunter2",)),
                         "<redacted> mail <email> token=<redacted> <redacted>")

    def test_empty_secret_is_ignored(self):
        self.assertEqual(claim_common.sanitize_detail("plain text", secrets=("", None)), "plain text")

    def test_first_non_empty_line_only(self):
        self.assertEqual(claim_common.sanitize_detail("\n  \n first \n second"), "first")

    def test_email_forms(self):
        for text in ("a@x.test", "first.last+tag@sub.x.test", "a%40x.test"):
            with self.subTest(text):
                self.assertEqual(claim_common.sanitize_detail(f"for {text} now"), "for <email> now")

    def test_each_named_key_is_redacted(self):
        for key in ("password", "passwd", "token", "secret", "authorization", "cookie",
                    "session", "sessionId", "session_token", "user_id", "userEmail",
                    "PASSWORD", "accessToken", "Set-Cookie"):
            with self.subTest(key):
                out = claim_common.sanitize_detail(f"before {key}=SENSITIVE")
                self.assertNotIn("SENSITIVE", out)
                self.assertIn("<redacted>", out)

    def test_key_value_shapes(self):
        for text in ('{"token":"SENSITIVE"}', "{'password': 'SENSITIVE'}", '"secret" : "SEN SITIVE"',
                     "token: SENSITIVE", "a=1&token=SENSITIVE&b=2", 'session="unterminated SENSITIVE'):
            with self.subTest(text):
                self.assertNotIn("SENSITIVE", claim_common.sanitize_detail(text))
                self.assertNotIn("SITIVE", claim_common.sanitize_detail(text))

    def test_header_values_are_redacted_to_the_end_of_the_line(self):
        for text in ("authorization: Bearer SENSITIVE", "cookie: a=SENSITIVE; b=SENSITIVE"):
            with self.subTest(text):
                self.assertNotIn("SENSITIVE", claim_common.sanitize_detail(text))

    def test_values_around_a_key_survive(self):
        self.assertEqual(claim_common.sanitize_detail("a=1, token=x, b=2"), "a=1, token=<redacted>, b=2")

    def test_runs_of_token_characters(self):
        self.assertEqual(claim_common.sanitize_detail("x" * 31), "x" * 31)
        self.assertEqual(claim_common.sanitize_detail("x" * 32), "<redacted>")
        self.assertEqual(claim_common.sanitize_detail("a/b+c=d.e%f-g_" * 4), "<redacted>")

    def test_secret_straddling_the_limit_leaves_no_fragment(self):
        secret = "S3cretValue99"
        out = claim_common.sanitize_detail(" " * 1 + "x " * 95 + secret + " tail", secrets=(secret,))
        self.assertNotIn("S3cr", out)
        self.assertLessEqual(len(out), 200)

    def test_token_straddling_the_limit_leaves_no_fragment(self):
        for pad in range(170, 200, 3):
            with self.subTest(pad=pad):
                out = claim_common.sanitize_detail("y " * (pad // 2) + self.TOKEN)
                self.assertNotIn("Ab3", out)
                self.assertLessEqual(len(out), 200)

    def test_key_value_straddling_the_limit_leaves_no_fragment(self):
        out = claim_common.sanitize_detail("y " * 96 + "token=SENSITIVE")
        self.assertNotIn("SEN", out)

    def test_custom_limit(self):
        self.assertEqual(claim_common.sanitize_detail("abcdef", limit=3), "abc")

    def test_non_string_and_empty_input_do_not_raise(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError("no")

        for value, expected in (("", ""), ("  \n ", ""), (None, "None"), (0, "0"), ({}, "{}"), ([], "[]"),
                                (Unprintable(), "<unprintable>")):
            with self.subTest(repr(value)[:30]):
                self.assertEqual(claim_common.sanitize_detail(value), expected)

    def test_non_string_secret_does_not_raise(self):
        self.assertIsInstance(claim_common.sanitize_detail("text", secrets=(5,)), str)

    def test_dict_value_with_a_secret_key(self):
        self.assertNotIn("SENSITIVE", claim_common.sanitize_detail({"token": "SENSITIVE"}))

    def test_long_unbroken_input_stays_fast(self):
        """It runs on the event loop every account shares, on text from the page or server,
        and truncation comes last: a slow pattern here stalls the whole run"""
        began = time.monotonic()
        claim_common.sanitize_detail("a" * 16000)
        self.assertLess(time.monotonic() - began, 1)


class JsTruthy(unittest.TestCase):
    def test_falsy_values_match_javascript(self):
        for value in (None, False, 0, 0.0, -0.0, ""):
            with self.subTest(repr(value)):
                self.assertFalse(claim_common.js_truthy(value))

    def test_truthy_values_match_javascript(self):
        for value in ({}, [], {"a": 1}, [0], "0", "false", " ", 1, -1, 0.5, True):
            with self.subTest(repr(value)):
                self.assertTrue(claim_common.js_truthy(value))


class SteppedLocator:
    """Fake locator: the count at any moment comes from a function of elapsed time"""

    def __init__(self, count_at):
        self.count_at = count_at
        self.began = time.monotonic()
        self.calls = 0

    async def count(self):
        self.calls += 1
        return self.count_at(time.monotonic() - self.began)


class WaitForStableCount(unittest.TestCase):
    def run_wait(self, count_at, stable_ms, timeout_ms):
        locator = SteppedLocator(count_at)
        began = time.monotonic()
        settled = asyncio.run(claim_common.wait_for_stable_count(locator, stable_ms, timeout_ms))
        return settled, time.monotonic() - began

    def test_constant_count_returns_true_after_the_window(self):
        settled, seconds = self.run_wait(lambda t: 3, 400, 5000)
        self.assertTrue(settled)
        self.assertGreaterEqual(seconds, 0.4)
        self.assertLessEqual(seconds, 0.9)

    def test_a_change_restarts_the_timer(self):
        settled, seconds = self.run_wait(lambda t: 1 if t < 0.3 else 2, 400, 5000)
        self.assertTrue(settled)
        self.assertGreaterEqual(seconds, 0.7)
        self.assertLessEqual(seconds, 1.3)

    def test_each_change_restarts_the_timer(self):
        settled, seconds = self.run_wait(lambda t: min(int(t / 0.25), 3), 400, 5000)
        self.assertTrue(settled)
        self.assertGreaterEqual(seconds, 1.15)

    def test_a_count_that_never_settles_returns_false_at_the_timeout(self):
        settled, seconds = self.run_wait(lambda t: int(t / 0.15), 400, 900)
        self.assertFalse(settled)
        self.assertGreaterEqual(seconds, 0.9)
        self.assertLessEqual(seconds, 1.4)

    def test_a_count_going_down_also_restarts_the_timer(self):
        settled, seconds = self.run_wait(lambda t: 3 if t < 0.3 else 2, 400, 5000)
        self.assertTrue(settled)
        self.assertGreaterEqual(seconds, 0.7)

    def test_zero_window_returns_at_once(self):
        settled, seconds = self.run_wait(lambda t: 1, 0, 5000)
        self.assertTrue(settled)
        self.assertLess(seconds, 0.2)

    def test_window_stays_above_the_gap_it_exists_to_catch(self):
        self.assertEqual(claim_common.CARD_COUNT_STABLE_MS, 1500)
        self.assertEqual(claim_common.CARD_COUNT_TIMEOUT_MS, 8000)


class FirstLine(unittest.TestCase):
    def test_keeps_only_the_first_line(self):
        self.assertEqual(claim_common.first_line(Exception("one\ntwo\nthree")), "one")

    def test_skips_leading_blank_lines(self):
        self.assertEqual(claim_common.first_line(Exception("\n\n  one\ntwo")), "one")

    def test_empty_message_falls_back_to_type(self):
        self.assertEqual(claim_common.first_line(ValueError("")), "ValueError")

    def test_blank_message_falls_back_to_type(self):
        self.assertEqual(claim_common.first_line(ValueError("  \n ")), "ValueError")


class FakeRoute:
    def __init__(self, url, resource_type, method="GET", failure=None):
        self.request = SimpleNamespace(url=url, resource_type=resource_type, method=method)
        self.failure = failure
        self.calls = []

    async def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.failure:
            raise self.failure

    async def abort(self, *args):
        await self._record("abort", {})

    async def continue_(self, **kwargs):
        await self._record("continue", kwargs)

    async def fulfill(self, **kwargs):
        await self._record("fulfill", kwargs)


APP = "https://app.test"


class BlockNonessential(unittest.TestCase):
    def decide(self, url, resource_type, method="GET"):
        route = FakeRoute(url, resource_type, method)
        asyncio.run(claim_common.block_nonessential(route))
        self.assertEqual(len(route.calls), 1, f"{url} must be handled exactly once")
        return route.calls[0]

    def test_font_and_media_abort(self):
        for resource_type in ("font", "media"):
            with self.subTest(resource_type):
                self.assertEqual(self.decide(f"{APP}/x", resource_type)[0], "abort")

    def test_image_fulfils_a_real_1x1_gif(self):
        name, kwargs = self.decide(f"{APP}/a.png", "image")
        self.assertEqual(name, "fulfill")
        self.assertEqual(kwargs["status"], 200)
        self.assertEqual(kwargs["content_type"], "image/gif")
        self.assertEqual(kwargs["headers"]["Access-Control-Allow-Origin"], "*")
        self.assertEqual(kwargs["body"][:3], b"GIF")
        self.assertEqual(int.from_bytes(kwargs["body"][6:8], "little"), 1)
        self.assertEqual(int.from_bytes(kwargs["body"][8:10], "little"), 1)

    def test_tracking_hosts_abort_for_every_resource_type(self):
        for host in ("www.googletagmanager.com", "googletagmanager.com", "google-analytics.com",
                     "www.google-analytics.com", "WWW.GoogleTagManager.COM"):
            for resource_type in ("script", "image", "xhr", "fetch", "document", "ping"):
                with self.subTest(host=host, resource_type=resource_type):
                    self.assertEqual(self.decide(f"https://{host}/x", resource_type)[0], "abort")

    def test_analytics_tunnel_fulfils_empty_json(self):
        for method, resource_type in (("POST", "fetch"), ("POST", "xhr"), ("POST", "ping")):
            with self.subTest(resource_type):
                name, kwargs = self.decide(f"{APP}/api/tunnel/analytics/post-event", resource_type, method)
                self.assertEqual(name, "fulfill")
                self.assertEqual(kwargs["status"], 200)
                self.assertEqual(kwargs["body"], "{}")

    def test_app_traffic_continues(self):
        for resource_type in ("document", "script", "stylesheet", "xhr", "fetch"):
            with self.subTest(resource_type):
                self.assertEqual(self.decide(f"{APP}/page", resource_type)[0], "continue")

    def test_flow_apis_continue(self):
        for path in ("/api/account/login", "/api/account/select-profile", "/api/appcharge/collect-offer"):
            with self.subTest(path):
                self.assertEqual(self.decide(f"{APP}{path}?locale=en-us", "fetch", "POST")[0], "continue")

    def test_third_parties_the_app_needs_continue(self):
        for url in ("https://cdn-ukwest.onetrust.com/x.js", "https://js.stripe.com/v3"):
            with self.subTest(url):
                self.assertEqual(self.decide(url, "script")[0], "continue")

    def test_lookalike_hosts_are_not_blocked(self):
        for url in ("https://notgoogletagmanager.com/gtm.js",
                    "https://googletagmanager.com.evil.test/gtm.js",
                    "https://not-google-analytics.com/c",
                    "https://googletagmanager.com@app.test/x",
                    f"{APP}/googletagmanager.com/x",
                    f"{APP}/x?host=google-analytics.com"):
            with self.subTest(url):
                self.assertEqual(self.decide(url, "script")[0], "continue")

    def test_closed_context_is_swallowed(self):
        for url, resource_type in ((f"{APP}/x", "font"), (f"{APP}/x", "image"), (f"{APP}/x", "script"),
                                   (f"{APP}/api/tunnel/analytics/e", "fetch")):
            with self.subTest(resource_type):
                route = FakeRoute(url, resource_type, failure=claim_common.PlaywrightError("closed"))
                asyncio.run(claim_common.block_nonessential(route))
                self.assertEqual(len(route.calls), 1)


class SecretRepr:
    """A future whose repr carries the secret, as Playwright's does on interrupt"""

    def __repr__(self):
        return '<Future fill("SECRET")>'


class SafeExceptionHandler(unittest.TestCase):
    CONTEXT = {
        "exception": claim_common.PlaywrightError('first\nfill("SECRET")'),
        "message": 'Future exception was never retrieved SECRET',
        "future": SecretRepr(),
    }

    def capture(self, call):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            call()
        return out.getvalue(), err.getvalue()

    def open_loop(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return loop

    def closed_loop(self):
        loop = asyncio.new_event_loop()
        loop.close()
        return loop

    def check_output(self, out, err):
        self.assertNotIn("SECRET", out + err)
        self.assertIn("Background error: Error: first", out)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(err, "")

    def test_direct_call_outputs_no_secret(self):
        for name, make in (("open", self.open_loop), ("closed", self.closed_loop)):
            with self.subTest(name):
                loop = make()
                self.check_output(*self.capture(
                    lambda: claim_common.safe_exception_handler(loop, dict(self.CONTEXT))))

    def test_through_the_loop_outputs_no_secret_and_skips_the_default_handler(self):
        for name, make in (("open", self.open_loop), ("closed", self.closed_loop)):
            with self.subTest(name):
                loop = make()
                loop.set_exception_handler(claim_common.safe_exception_handler)
                with unittest_mock.patch.object(loop, "default_exception_handler") as default:
                    self.check_output(*self.capture(lambda: loop.call_exception_handler(dict(self.CONTEXT))))
                default.assert_not_called()

    def test_a_real_unretrieved_future_outputs_no_secret(self):
        for name, closing in (("open", False), ("closed", True)):
            with self.subTest(name):
                loop = asyncio.new_event_loop()
                self.addCleanup(loop.close)
                loop.set_exception_handler(claim_common.safe_exception_handler)

                def abandon():
                    future = loop.create_future()
                    future.set_exception(claim_common.PlaywrightError('first\nfill("SECRET")'))
                    if closing:
                        loop.close()
                    del future
                    gc.collect()

                self.check_output(*self.capture(abandon))

    def test_no_exception_in_context(self):
        loop = self.open_loop()
        out, err = self.capture(lambda: claim_common.safe_exception_handler(
            loop, {"message": "SECRET", "task": SecretRepr()}))
        self.assertRegex(out, r"\[WARNING\] Background error\n$")
        self.assertNotIn("SECRET", out + err)

    def test_log_raising_is_swallowed_and_the_default_handler_stays_out(self):
        for name, make in (("open", self.open_loop), ("closed", self.closed_loop)):
            with self.subTest(name):
                loop = make()
                loop.set_exception_handler(claim_common.safe_exception_handler)
                with unittest_mock.patch.object(claim_common, "log", side_effect=OSError("stdout gone")), \
                        unittest_mock.patch.object(loop, "default_exception_handler") as default:
                    claim_common.safe_exception_handler(loop, dict(self.CONTEXT))
                    out, err = self.capture(lambda: loop.call_exception_handler(dict(self.CONTEXT)))
                default.assert_not_called()
                self.assertNotIn("SECRET", out + err)

    def test_exception_whose_str_raises_is_swallowed(self):
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("SECRET")

        loop = self.open_loop()
        out, err = self.capture(lambda: claim_common.safe_exception_handler(loop, {"exception": Unprintable()}))
        self.assertNotIn("SECRET", out + err)


class CountingRouteContext:
    def __init__(self):
        self.routes = 0

    async def route(self, pattern, handler):
        self.routes += 1

    async def new_page(self):
        return "page"

    async def close(self):
        pass


class AccountPageBlocking(unittest.TestCase):
    def run_with(self, block_resources):
        context = CountingRouteContext()
        holder = FakeHolder(context)
        holder.block_resources = block_resources

        async def main():
            async with claim_common.account_page(holder, "a@x.test"):
                pass

        asyncio.run(main())
        return context.routes

    def test_route_installed_by_default(self):
        self.assertEqual(self.run_with(True), 1)

    def test_route_skipped_when_blocking_is_off(self):
        self.assertEqual(self.run_with(False), 0)


class FakeContext:
    def __init__(self, close_error=None, close_hangs=False):
        self.close_error = close_error
        self.close_hangs = close_hangs
        self.closes = 0

    async def route(self, pattern, handler):
        pass

    async def new_page(self):
        return "page"

    async def close(self):
        self.closes += 1
        if self.close_hangs:
            await asyncio.Event().wait()
        if self.close_error:
            raise self.close_error


class FakeHolder:
    block_resources = True

    def __init__(self, context):
        self.context = context

    async def get(self):
        return self

    async def new_context(self):
        return self.context


class AccountPageClose(unittest.TestCase):
    EMAIL = "closing@x.test"

    def use_page(self, context, body=None):
        async def main():
            async with claim_common.account_page(FakeHolder(context), self.EMAIL) as page:
                if body:
                    await body()
                return page

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return asyncio.run(main()), out.getvalue()

    def test_default_limit_is_30_seconds(self):
        self.assertEqual(claim_common.CONTEXT_CLOSE_TIMEOUT_SECONDS, 30)

    def test_hung_close_is_bounded_keeps_the_result_and_names_the_account(self):
        began = time.monotonic()
        with unittest_mock.patch.object(claim_common, "CONTEXT_CLOSE_TIMEOUT_SECONDS", 0.1):
            result, out = self.use_page(FakeContext(close_hangs=True))
        self.assertEqual(result, "page")
        self.assertLess(time.monotonic() - began, 3)
        self.assertRegex(out, rf"\[WARNING\] \[{self.EMAIL}\] Error closing browser context: TimeoutError\n")

    def test_raising_close_keeps_the_result_on_one_line(self):
        result, out = self.use_page(FakeContext(close_error=RuntimeError("close failed\nsecond")))
        self.assertEqual(result, "page")
        self.assertIn(f"[{self.EMAIL}] Error closing browser context: close failed", out)
        self.assertNotIn("second", out)

    def test_hung_close_does_not_replace_an_error_from_the_body(self):
        async def body():
            raise ValueError("from the account")

        with unittest_mock.patch.object(claim_common, "CONTEXT_CLOSE_TIMEOUT_SECONDS", 0.1):
            with self.assertRaisesRegex(ValueError, "from the account"):
                self.use_page(FakeContext(close_hangs=True), body)

    def test_cancellation_during_a_hung_close_is_not_swallowed(self):
        async def main():
            async def use():
                async with claim_common.account_page(FakeHolder(FakeContext(close_hangs=True)), self.EMAIL):
                    pass

            task = asyncio.create_task(use())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            task.cancel()
            await task

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(main())


class KillDriver(unittest.IsolatedAsyncioTestCase):
    def manager_for(self, proc):
        return SimpleNamespace(_connection=SimpleNamespace(_transport=SimpleNamespace(_proc=proc)))

    async def test_kills_a_running_process(self):
        proc = await asyncio.create_subprocess_exec("/bin/sleep", "30")
        claim_common.kill_driver(self.manager_for(proc))
        self.assertIsNotNone(await asyncio.wait_for(proc.wait(), 5))

    async def test_already_exited_process_is_not_an_error(self):
        proc = await asyncio.create_subprocess_exec("/usr/bin/true")
        await proc.wait()
        claim_common.kill_driver(self.manager_for(proc))

    async def test_missing_private_attributes_are_not_an_error(self):
        for manager in (object(), SimpleNamespace(_connection=None),
                        SimpleNamespace(_connection=SimpleNamespace(_transport=SimpleNamespace()))):
            with self.subTest(manager):
                claim_common.kill_driver(manager)


class FakeBrowser:
    def __init__(self, close_error=None, close_hangs=False):
        self.closed = False
        self.close_error = close_error
        self.close_hangs = close_hangs

    def is_connected(self):
        return not self.closed

    async def close(self):
        if self.close_hangs:
            await asyncio.Event().wait()
        self.closed = True
        if self.close_error:
            raise self.close_error


class FakePlaywright:
    """Stands in for the browser process, so run_accounts itself runs for real"""

    def __init__(self, close_error=None, close_hangs=False, start_error=None, exit_error=None, exit_hangs=False):
        self.launches = 0
        self.browsers = []
        self.chromium = self
        self.close_error = close_error
        self.close_hangs = close_hangs
        self.start_error = start_error
        self.exit_error = exit_error
        self.exit_hangs = exit_hangs
        self.handler_at_start = None

    async def launch(self, **kwargs):
        self.launches += 1
        self.browsers.append(FakeBrowser(self.close_error, self.close_hangs))
        return self.browsers[-1]

    async def __aenter__(self):
        self.handler_at_start = asyncio.get_running_loop().get_exception_handler()
        if self.start_error:
            raise self.start_error
        return self

    async def __aexit__(self, *args):
        if self.exit_hangs:
            await asyncio.Event().wait()
        if self.exit_error:
            raise self.exit_error
        return False


def run_fake(accounts, worker, concurrency, stagger=0, failure_result=lambda a: ("failed", a["email"]), fake=None):
    fake = fake or FakePlaywright()
    options = SimpleNamespace(concurrency=concurrency, stagger=stagger)
    with unittest_mock.patch.object(claim_common, "async_playwright", lambda: fake), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        results = asyncio.run(claim_common.run_accounts(accounts, worker, failure_result, options))
    return results, fake, out.getvalue()


def accounts(count):
    return [{"email": f"a{i}@x.test", "password": "p"} for i in range(count)]


class RunAccounts(unittest.TestCase):
    def test_results_keep_input_order_when_workers_finish_out_of_order(self):
        async def worker(browsers, account, number, total):
            await asyncio.sleep((total - number) * 0.03)
            return account["email"]

        results, _, _ = run_fake(accounts(5), worker, 5)
        self.assertEqual(results, [a["email"] for a in accounts(5)])

    def test_peak_is_exactly_the_limit(self):
        for limit in (1, 2, 3, 5):
            with self.subTest(limit):
                active, peak = 0, 0

                async def worker(browsers, account, number, total):
                    nonlocal active, peak
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.02)
                    active -= 1

                run_fake(accounts(7), worker, limit)
                self.assertEqual(peak, limit)

    def test_worker_gets_account_position_and_total(self):
        seen = []

        async def worker(browsers, account, number, total):
            seen.append((account["email"], number, total))

        run_fake(accounts(2), worker, 1)
        self.assertEqual(seen, [("a0@x.test", 1, 2), ("a1@x.test", 2, 2)])

    def test_raising_worker_fails_alone_and_is_logged_with_email(self):
        async def worker(browsers, account, number, total):
            await asyncio.sleep(0.01)
            if number == 2:
                raise ValueError("first\nsecond")
            return "ok"

        results, _, out = run_fake(accounts(3), worker, 2)
        self.assertEqual(results, ["ok", ("failed", "a1@x.test"), "ok"])
        self.assertIn("Error processing a1@x.test: first", out)
        self.assertNotIn("second", out)

    def test_every_start_is_paced_including_after_a_finish(self):
        starts = []

        async def worker(browsers, account, number, total):
            starts.append(time.monotonic())

        run_fake(accounts(6), worker, 2, stagger=0.1)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(len(gaps), 5)
        self.assertGreaterEqual(min(gaps), 0.095, gaps)

    def test_first_start_is_not_delayed(self):
        starts = []

        async def worker(browsers, account, number, total):
            starts.append(time.monotonic())

        began = time.monotonic()
        run_fake(accounts(1), worker, 1, stagger=30)
        self.assertLess(starts[0] - began, 1)

    def test_browser_launches_once_and_closes(self):
        async def worker(browsers, account, number, total):
            await browsers.get()

        _, fake, _ = run_fake(accounts(5), worker, 3)
        self.assertEqual(fake.launches, 1)
        self.assertTrue(fake.browsers[0].closed)

    def test_handler_is_installed_inside_a_worker(self):
        seen = []

        async def worker(browsers, account, number, total):
            seen.append(asyncio.get_running_loop().get_exception_handler())

        run_fake(accounts(2), worker, 1)
        self.assertEqual(seen, [claim_common.safe_exception_handler] * 2)

    def test_handler_is_installed_before_playwright_starts(self):
        async def worker(browsers, account, number, total):
            pass

        _, fake, _ = run_fake(accounts(1), worker, 1)
        self.assertIs(fake.handler_at_start, claim_common.safe_exception_handler)

    def test_disconnected_browser_is_relaunched(self):
        async def worker(browsers, account, number, total):
            browser = await browsers.get()
            if number == 1:
                browser.closed = True

        _, fake, _ = run_fake(accounts(3), worker, 1)
        self.assertEqual(fake.launches, 2)


class RunAccountsTeardown(unittest.TestCase):
    async def worker(self, browsers, account, number, total):
        await browsers.get()
        return "ok"

    def test_browser_close_raising_keeps_results(self):
        fake = FakePlaywright(close_error=RuntimeError("close failed\nsecond"))
        results, _, out = run_fake(accounts(2), self.worker, 2, fake=fake)
        self.assertEqual(results, ["ok", "ok"])
        self.assertRegex(out, r"\[WARNING\] Error closing browser: close failed\n")
        self.assertNotIn("second", out)

    def test_browser_close_hanging_is_bounded(self):
        fake = FakePlaywright(close_hangs=True)
        began = time.monotonic()
        with unittest_mock.patch.object(claim_common, "BROWSER_CLOSE_TIMEOUT_SECONDS", 0.1):
            results, _, out = run_fake(accounts(1), self.worker, 1, fake=fake)
        self.assertEqual(results, ["ok"])
        self.assertLess(time.monotonic() - began, 5)
        self.assertIn("[WARNING] Error closing browser: TimeoutError", out)

    def test_default_bounds(self):
        self.assertEqual(claim_common.BROWSER_CLOSE_TIMEOUT_SECONDS, 30)
        self.assertEqual(claim_common.PLAYWRIGHT_STOP_TIMEOUT_SECONDS, 5)

    def test_playwright_exit_hanging_is_bounded_and_kills_the_driver(self):
        fake = FakePlaywright(exit_hangs=True)
        began = time.monotonic()
        with unittest_mock.patch.object(claim_common, "PLAYWRIGHT_STOP_TIMEOUT_SECONDS", 0.1), \
                unittest_mock.patch.object(claim_common, "kill_driver") as kill:
            results, _, out = run_fake(accounts(1), self.worker, 1, fake=fake)
        self.assertEqual(results, ["ok"])
        self.assertLess(time.monotonic() - began, 5)
        self.assertIn("[WARNING] Error stopping Playwright: TimeoutError", out)
        kill.assert_called_once_with(fake)

    def test_driver_is_killed_after_a_clean_stop_too(self):
        fake = FakePlaywright()
        with unittest_mock.patch.object(claim_common, "kill_driver") as kill:
            run_fake(accounts(1), self.worker, 1, fake=fake)
        kill.assert_called_once_with(fake)

    def test_cancellation_during_a_hung_stop_is_not_swallowed_and_kills_the_driver(self):
        async def main():
            options = SimpleNamespace(concurrency=1, stagger=0)
            task = asyncio.create_task(claim_common.run_accounts(accounts(1), self.worker, lambda a: None, options))
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            task.cancel()
            await task

        fake = FakePlaywright(exit_hangs=True)
        with unittest_mock.patch.object(claim_common, "async_playwright", lambda: fake), \
                unittest_mock.patch.object(claim_common, "kill_driver") as kill, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(main())
        kill.assert_called_once_with(fake)

    def test_exit_raising_does_not_replace_a_cancellation(self):
        async def stuck(browsers, account, number, total):
            await browsers.get()
            await asyncio.Event().wait()

        async def main():
            options = SimpleNamespace(concurrency=1, stagger=0)
            task = asyncio.create_task(claim_common.run_accounts(accounts(1), stuck, lambda a: None, options))
            await asyncio.sleep(0.05)
            task.cancel()
            await task

        fake = FakePlaywright(exit_error=RuntimeError("stop failed"))
        with unittest_mock.patch.object(claim_common, "async_playwright", lambda: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(main())

    def test_playwright_exit_raising_keeps_results(self):
        fake = FakePlaywright(exit_error=RuntimeError("stop failed\nsecond"))
        results, _, out = run_fake(accounts(1), self.worker, 1, fake=fake)
        self.assertEqual(results, ["ok"])
        self.assertRegex(out, r"\[WARNING\] Error stopping Playwright: stop failed\n")

    def test_playwright_start_failure_is_reraised(self):
        fake = FakePlaywright(start_error=RuntimeError("start failed"))
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            run_fake(accounts(1), self.worker, 1, fake=fake)

    def test_cancellation_during_a_hung_close_is_not_swallowed(self):
        """Ctrl+C while teardown is waiting: the accounts are done, and it must still win"""
        async def main():
            options = SimpleNamespace(concurrency=1, stagger=0)
            task = asyncio.create_task(claim_common.run_accounts(accounts(1), self.worker, lambda a: None, options))
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            task.cancel()
            await task

        fake = FakePlaywright(close_hangs=True)
        with unittest_mock.patch.object(claim_common, "async_playwright", lambda: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(main())

    def test_cancellation_is_not_swallowed_by_a_failing_close(self):
        async def stuck(browsers, account, number, total):
            await browsers.get()
            await asyncio.Event().wait()

        async def main(fake):
            options = SimpleNamespace(concurrency=1, stagger=0)
            task = asyncio.create_task(claim_common.run_accounts(accounts(1), stuck, lambda a: None, options))
            await asyncio.sleep(0.05)
            task.cancel()
            await task

        fake = FakePlaywright(close_error=RuntimeError("close failed"))
        with unittest_mock.patch.object(claim_common, "async_playwright", lambda: fake), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(main(fake))


if __name__ == "__main__":
    unittest.main()
