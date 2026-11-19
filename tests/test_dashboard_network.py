"""Dashboard N5: internet status. Everything stays on loopback: Dash runs under the audit hook,
so a socket attempt that leaves is blocked before DNS, recorded, and fails the test at cleanup."""

import fcntl
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock as unittest_mock

from dash_support import (
    DASH_HARNESS,
    DashTestCase,
    Stub,
    account_end,
    assert_safe_dashboard_env,
    event,
    run_start,
    script_env,
    wait_online,
    write_events,
)
from gow_support import PROJECT, PYTHON, Account, UnsafeRun, wait_until
from test_dashboard_page import PageCase

from dashboard import network

FAST = {"default_stagger": 0.1}
QUICK = {"INTERNET_TIMEOUT_SECONDS": 0.3, "SITE_TIMEOUT_SECONDS": 0.3, "HARD_DEADLINE_EXTRA_SECONDS": 0.3}
EMAIL = "net@x.test"
SLOW = Account(vault_delay_ms=25000)
KEYS = {"state", "checked_seconds_ago", "internet_ms", "site_ms", "site_status", "failure"}
FAILURES = (None, "dns", "timeout", "refused", "tls", "http", "error")


def closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Net(DashTestCase):
    def net(self, stub=None, interval=0.5, head_status=200, head_hang=False, accounts=None, constants=None,
            harness=None, **kwargs):
        config = {**(harness or {}), "net_constants": constants or {}}
        dash = self.dash(accounts or {}, stub=stub, net_interval=interval, harness=config, **kwargs)
        dash.mock.head_status = head_status
        dash.mock.head_hang = head_hang
        return dash

    def network(self, dash):
        return dash.state()["network"]

    def wait_network(self, dash, state, timeout=8):
        return dash.wait_state(lambda s: s["network"]["state"] == state, timeout, f"network {state}")["network"]


class States(Net):
    def test_net1_ok(self):
        dash = self.net(Stub(self))
        network_state = self.wait_network(dash, "ok")
        self.assertEqual(set(network_state), KEYS)
        self.assertIsInstance(network_state["internet_ms"], int)
        self.assertIsInstance(network_state["site_ms"], int)
        self.assertIsInstance(network_state["checked_seconds_ago"], (int, float))
        self.assertEqual(network_state["site_status"], 200)
        self.assertIsNone(network_state["failure"])

    def test_net2_no_internet_in_every_form(self):
        for mode, failure in (("refused", "refused"), ("200", "http"), ("302", "http"), ("hang", "timeout")):
            with self.subTest(mode):
                stub = Stub(self, mode)
                dash = self.net(stub, constants=QUICK)
                state = self.wait_network(dash, "no_internet")
                self.assertEqual(state["failure"], failure)
                self.assertEqual(dash.mock.head_requests, [])
                # Null measurements, as the Coder's deviation says
                self.assertIsNone(state["internet_ms"])
                self.assertIsNone(state["site_ms"])
                self.assertIsNone(state["site_status"])
                self.assertIsInstance(state["checked_seconds_ago"], (int, float))
                self.assertEqual([r for r in stub.requests if r["path"] == "/followed"], [])

    def test_net3_site_unreachable(self):
        for status in (500, 503):
            with self.subTest(status):
                dash = self.net(Stub(self), head_status=status)
                state = self.wait_network(dash, "site_unreachable")
                self.assertEqual(state["site_status"], status)
                self.assertEqual(state["failure"], "http")
                self.assertIsNone(state["site_ms"])

    def test_net3_site_never_answering(self):
        dash = self.net(Stub(self), head_hang=True, constants=QUICK)
        state = self.wait_network(dash, "site_unreachable")
        self.assertIsNone(state["site_status"])
        self.assertEqual(state["failure"], "timeout")

    def test_net3_site_refusing(self):
        dash = self.net(Stub(self), base_url=f"http://127.0.0.1:{closed_port()}/en-us")
        state = self.wait_network(dash, "site_unreachable")
        self.assertEqual(state["failure"], "refused")
        self.assertIsNone(state["site_status"])

    def test_net3_any_answer_below_500_is_reachable(self):
        for status in (403, 404, 302):
            with self.subTest(status):
                dash = self.net(Stub(self), head_status=status)
                state = self.wait_network(dash, "ok")
                self.assertEqual(state["site_status"], status)

    def test_net4_checking_with_both_targets_held(self):
        dash = self.net(Stub(self, "hang"), head_hang=True)
        for _ in range(5):
            state = self.network(dash)
            self.assertEqual(state["state"], "checking")
            self.assertIsNone(state["checked_seconds_ago"])
            self.assertIsNone(state["internet_ms"])
            self.assertIsNone(state["site_ms"])
            self.assertIsNone(state["site_status"])
            time.sleep(0.2)

    def test_net4_checking_with_the_site_result_pending(self):
        stub = Stub(self)
        dash = self.net(stub, head_hang=True)
        self.network(dash)
        wait_until(lambda: stub.count() >= 1, 5, "the internet check")
        time.sleep(0.3)
        state = self.network(dash)
        self.assertEqual(state["state"], "checking")
        self.assertIsNone(state["internet_ms"])
        self.assertIsNone(state["site_ms"])

    def test_net5_an_old_result_stops_reading_ok(self):
        stub = Stub(self)
        dash = self.net(stub)
        self.wait_network(dash, "ok")
        stub.set("hang")
        dash.mock.head_hang = True
        self.wait_network(dash, "checking", timeout=4)

    def test_net5_control_without_staleness_it_keeps_the_old_ok(self):
        stub = Stub(self)
        dash = self.net(stub, harness={"no_net_stale": True})
        self.wait_network(dash, "ok")
        stub.set("hang")
        dash.mock.head_hang = True
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.assertEqual(self.network(dash)["state"], "ok")
            time.sleep(0.2)

    def test_net7_one_failure_holds_and_a_second_changes_it(self):
        stub = Stub(self)
        dash = self.net(stub, interval=1)
        self.wait_network(dash, "ok")
        before = stub.count()
        stub.fail_next = 1
        wait_until(lambda: stub.count() >= before + 2, 6, "the check and its recheck")
        self.assertEqual(self.network(dash)["state"], "ok")

        stub.fail_next = 2
        self.wait_network(dash, "no_internet", timeout=6)

    def test_net7_a_single_failure_never_shows(self):
        stub = Stub(self)
        dash = self.net(stub, interval=0.5)
        self.wait_network(dash, "ok")
        stub.fail_next = 1
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            self.assertEqual(self.network(dash)["state"], "ok")
            time.sleep(0.05)

    def test_net7_a_first_ever_failure_shows_at_once(self):
        stub = Stub(self, "200")
        dash = self.net(stub, interval=30)
        self.wait_network(dash, "no_internet", timeout=4)
        self.assertEqual(stub.count(), 1)

    def test_net9_a_restart_goes_back_to_checking(self):
        stub = Stub(self)
        dash = self.net(stub)
        self.wait_network(dash, "ok")
        dash.kill_server()
        dash.start()
        self.assertEqual(self.network(dash)["state"], "checking")
        self.wait_network(dash, "ok")
        for root, _, names in os.walk(dash.temp_dir):
            for name in names:
                if name.startswith(("spy", "net-threads", "server-", "fake-accounts")):
                    continue
                with open(os.path.join(root, name), errors="replace") as f:
                    text = f.read()
                self.assertNotIn("no_internet", text)
                self.assertNotIn("internet_ms", text)

    def test_net11_the_head_request_carries_nothing(self):
        dash = self.net(Stub(self))
        self.wait_network(dash, "ok")
        (first, *_) = dash.mock.head_requests
        self.assertEqual(first["path"], "/en-us/login")
        self.assertEqual(first["query"], "")
        self.assertEqual(first["headers"]["User-Agent"], "GoW-Dashboard/1")
        names = {name.lower() for name in first["headers"]}
        self.assertNotIn("cookie", names)
        self.assertNotIn("authorization", names)
        self.assertEqual(first["body"], b"")
        self.assertEqual(dash.mock.login_hits, [], "a check is not a login-page hit")

    def test_deviation_the_internet_check_uses_get_and_reads_no_account_data(self):
        stub = Stub(self)
        dash = self.net(stub)
        self.wait_network(dash, "ok")
        first = stub.requests[0]
        self.assertEqual(first["method"], "GET")
        self.assertEqual(first["headers"]["User-Agent"], "GoW-Dashboard/1")
        self.assertNotIn("Cookie", first["headers"])

    def proxy_env(self, trap):
        url = f"http://127.0.0.1:{trap.port}"
        return {"HTTP_PROXY": url, "http_proxy": url, "HTTPS_PROXY": url, "https_proxy": url, "NO_PROXY": "", "no_proxy": ""}

    def test_net11_a_proxy_receives_nothing(self):
        trap = Stub(self)
        dash = self.net(Stub(self), extra_env=self.proxy_env(trap))
        self.wait_network(dash, "ok")
        self.assertEqual(trap.count(), 0)

    def test_net11_control_the_trap_catches_a_check_that_uses_the_proxy(self):
        trap = Stub(self)
        dash = self.net(Stub(self), extra_env=self.proxy_env(trap), harness={"net_env_proxy": True})
        self.network(dash)
        wait_until(lambda: trap.count() >= 1, 6, "the trap to see a check")


class HungChecks(Net):
    def poll_times(self, dash, seconds):
        times = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            began = time.monotonic()
            dash.state()
            times.append(time.monotonic() - began)
            time.sleep(0.05)
        return times

    def test_net8_sockets_never_answering(self):
        dash = self.net(Stub(self, "hang"), head_hang=True, constants=QUICK)
        began = time.monotonic()
        self.wait_network(dash, "no_internet", timeout=2)
        self.assertLessEqual(time.monotonic() - began, 0.3 + 0.3 + 0.5)
        self.assertLess(max(self.poll_times(dash, 3)), 0.3)

    def test_net8_a_check_hanging_past_the_socket_timeout(self):
        dash = self.net(Stub(self), constants=QUICK, harness={"net_check_hangs": True})
        began = time.monotonic()
        self.wait_network(dash, "no_internet", timeout=2)
        self.assertLessEqual(time.monotonic() - began, 0.3 + 0.3 + 0.5)
        times = self.poll_times(dash, 6)
        self.assertLess(max(times), 0.3)
        self.assertLessEqual(dash.most_check_threads(), 3)
        self.assertGreaterEqual(dash.most_check_threads(), 3, "the limit has to be reached to mean anything")

    def test_net8_control_a_check_inside_the_request_breaks_the_limit(self):
        dash = self.net(Stub(self), constants=QUICK, harness={"net_check_hangs": True, "net_check_in_request": True})
        began = time.monotonic()
        dash.state()
        self.assertGreater(time.monotonic() - began, 0.3)


class Cadence(Net):
    IDLE = {"VIEWER_WINDOW_SECONDS": 1, "IDLE_POLL_SECONDS": 0.2}

    def test_net10_no_viewer_and_no_run_means_no_checks_and_the_next_poll_checks_at_once(self):
        stub = Stub(self)
        dash = self.net(stub, constants=self.IDLE)
        self.wait_network(dash, "ok")
        time.sleep(1.8)         # past the viewer window, and one poll for it to settle
        internet, site = stub.count(), len(dash.mock.head_requests)
        time.sleep(2.5)
        self.assertEqual(stub.count(), internet)
        self.assertEqual(len(dash.mock.head_requests), site)

        dash.state()
        wait_until(lambda: stub.count() > internet, 1, "an immediate check")

    def test_net10_a_terminal_run_keeps_checks_going_with_no_page_open(self):
        stub = Stub(self)
        dash = self.net(stub, constants=self.IDLE, accounts={EMAIL: SLOW})
        self.terminal_run(dash)
        wait_until(lambda: dash.mock.login_hits, 20, "the terminal run")
        started = stub.count()
        wait_until(lambda: stub.count() > started + 2, 6, "checks with nothing polling")
        self.assertGreater(stub.count(), 0)

    def test_net10_the_site_is_checked_at_most_once_per_4_internet_checks(self):
        stub = Stub(self)
        dash = self.net(stub, interval=0.5)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            self.network(dash)
            time.sleep(0.3)
        internet, site = stub.count(), len(dash.mock.head_requests)
        self.assertGreaterEqual(internet, 8)
        self.assertLessEqual(site, internet // 4 + 1)
        self.assertGreaterEqual(site, 2)

    def test_recovery_checks_the_site_at_once(self):
        stub = Stub(self, "200")
        dash = self.net(stub, interval=0.5)
        self.wait_network(dash, "no_internet")
        self.assertEqual(dash.mock.head_requests, [])
        stub.set("204")
        self.wait_network(dash, "ok", timeout=3)


class ClaimsUnaffected(Net):
    ACCOUNTS = {"alpha@x.test": Account(), "bravo@x.test": Account(vault="cooldown"),
                "charlie@x.test": Account(password="other")}

    def fingerprint(self, dash):
        state = dash.wait_state(lambda s: s["status"] == "finished" and not s["control"]["owned"], timeout=60,
                                message="the run")
        stages = [(e["line"], e["stage"]) for e in dash.recorded() if e["event"] == "stage"]
        totals = {key: state["totals"][key] for key in ("claimed", "already", "failed", "jewels", "characters")}
        return sorted(dash.mock.ledger_pairs()), totals, dash.child_exit(), stages

    def run_in(self, mode, head_status=200, head_hang=False):
        stub = Stub(self, mode)
        dash = self.net(stub, head_status=head_status, head_hang=head_hang, accounts=self.ACCOUNTS,
                        child_harness=FAST, constants=QUICK)
        self.assertEqual(dash.run(1), 200)
        return self.fingerprint(dash)

    def test_net12_same_result_in_every_state_by_direct_post(self):
        baseline = self.run_in("204")
        self.assertEqual(baseline[0], [("alpha@x.test", "only")])
        self.assertEqual(baseline[2], "1")
        for name, arguments in (("no internet", ("200",)), ("site unreachable", ("204", 500)),
                                ("hung checks", ("hang", 200, True))):
            with self.subTest(name):
                self.assertEqual(self.run_in(*arguments), baseline)

    def test_net12_static_no_claim_code_imports_network(self):
        for name in ("claim_common.py", "claim_all_characters.py", "claim_all.py", "claim_batch.py",
                     os.path.join("dashboard", "events.py"), os.path.join("dashboard", "launcher.py"),
                     os.path.join("dashboard", "service_helper.py")):
            with self.subTest(name):
                with open(os.path.join(PROJECT, name), encoding="utf-8") as f:
                    text = f.read()
                self.assertNotRegex(text, r"(?m)^\s*(from|import)\s+.*\bnetwork\b")

    def test_net12_the_child_never_loads_the_module(self):
        accounts = {EMAIL: Account()}
        dash = self.net(Stub(self), accounts=accounts, child_harness=FAST)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["status"] == "finished", timeout=40, message="the run")
        wait_until(lambda: os.path.exists(dash.child_spy), 5, "the child's spy")
        with open(dash.child_spy) as f:
            self.assertIs(json.load(f)["network_loaded"], False)


class Secrets(Net):
    def test_net15_six_keys_and_nothing_from_the_url(self):
        secret = "SECRETPASS"
        for mode in ("refused", "hang", "200", "302", "204"):
            with self.subTest(mode):
                stub = Stub(self, "204")
                url = f"http://user:{secret}@127.0.0.1:{stub.port}/check?token=SEEDTOKEN"
                stub.set(mode)
                dash = self.net(stub, internet_url=url, constants=QUICK)
                self.network(dash)
                time.sleep(1.2)
                blobs = [dash.get(path)[2].decode() + str(dash.get(path)[1])
                         for path in ("/", "/app.js", "/app.css", "/nope")]
                state_text = dash.get("/api/state")[2].decode()
                state = json.loads(state_text)["network"]
                self.assertEqual(set(state), KEYS)
                self.assertIn(state["failure"], FAILURES)
                blobs += [state_text, dash.output()]
                for path, payload in (("/api/stop", {}), ("/api/run", {"concurrency": 9})):
                    blobs.append(dash.post(path, payload)[2].decode())
                joined = "\n".join(blobs)
                for leaked in (secret, "SEEDTOKEN", "user:", "/check"):
                    self.assertNotIn(leaked, joined)
                self.assertNotIn("127.0.0.1", state_text)
                self.assertNotIn("Error", state_text)

    def test_deviation_credentials_are_never_sent(self):
        stub = Stub(self)
        url = f"http://user:SECRETPASS@127.0.0.1:{stub.port}/check?token=SEEDTOKEN"
        dash = self.net(stub, internet_url=url)
        self.wait_network(dash, "ok")
        first = stub.requests[0]
        self.assertEqual(first["path"], "/check?token=SEEDTOKEN")
        self.assertNotIn("Authorization", first["headers"])
        self.assertEqual(first["headers"]["Host"], f"127.0.0.1:{stub.port}")

    def test_net15_failure_of_tls_stays_in_the_enum(self):
        stub = Stub(self)
        dash = self.net(stub, internet_url=f"https://127.0.0.1:{stub.port}/check", constants=QUICK)
        state = self.wait_network(dash, "no_internet")
        self.assertIn(state["failure"], ("tls", "error"))


class Routes(Net):
    def test_net16(self):
        dash = self.net(Stub(self))
        self.assertEqual(dash.get("/api/network")[0], 404)
        host = ("Host", dash.host())
        for origin in ("https://evil.test", "null", "http://127.0.0.1:1"):
            with self.subTest(origin):
                self.assertEqual(dash.request("GET", "/api/state", headers=[host, ("Origin", origin)])[0], 403)
        self.assertEqual(dash.request("GET", "/api/state", headers=[host])[0], 200)
        own = ("Origin", f"http://{dash.host()}")
        status, headers, _ = dash.request("GET", "/api/state", headers=[host, own])
        self.assertEqual(status, 200)
        self.assert_headers(headers)
        twice = [host, own, ("Origin", "https://evil.test")]
        self.assertEqual(dash.request("GET", "/api/state", headers=twice)[0], 403)
        self.assertEqual(dash.get("/api/state", host="evil.test")[0], 403)

    def test_server_id_is_new_in_the_state_and_not_the_token(self):
        dash = self.net()
        state = dash.state()
        self.assertRegex(state["server_id"], r"^[0-9a-f]{16}$")
        self.assertNotEqual(state["server_id"], dash.token)
        self.assertNotIn(dash.token, json.dumps(state))
        first = state["server_id"]
        dash.kill_server()
        dash.start()
        self.assertNotEqual(dash.state()["server_id"], first)


class Validation(DashTestCase):
    def refused(self, value, name="GOW_INTERNET_CHECK_URL"):
        mock = self.make_mock()
        temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", temp]))
        env = script_env(mock, os.path.join(temp, "fake-accounts"))
        env["GOW_EVENTS_FILE"] = os.path.join(temp, "data", "events.jsonl")
        env.pop("GOW_INTERNET_CHECK_URL", None)
        env[name] = value
        env["GOW_DASH_HARNESS"] = json.dumps({"spy_file": os.path.join(temp, "spy"),
                                              "net_audit_file": os.path.join(temp, "audit")})
        done = subprocess.run([PYTHON, "-B", DASH_HARNESS, "--port", "0"], cwd=PROJECT, env=env,
                              capture_output=True, text=True, timeout=20)
        self.assertFalse(os.path.exists(os.path.join(temp, "audit")))
        return done

    def test_net17_bad_targets_stop_the_server_before_it_listens(self):
        for value in ("file:///etc/passwd", "ftp://127.0.0.1/", "text with no hostname", "http://", "http://:80/x"):
            with self.subTest(value):
                done = self.refused(value)
                self.assertEqual(done.returncode, 1)
                self.assertRegex(done.stdout, r"(?m)^Error: ")
                self.assertNotIn("Dashboard:", done.stdout)
                self.assertNotIn(value, done.stdout + done.stderr)

    def test_deviation_an_empty_url_is_an_error(self):
        done = self.refused("")
        self.assertEqual(done.returncode, 1)
        self.assertRegex(done.stdout, r"(?m)^Error: GOW_INTERNET_CHECK_URL")

    def test_a_bad_base_url_is_an_error_too(self):
        done = self.refused("ftp://127.0.0.1/en-us", "GOW_BASE_URL")
        self.assertEqual(done.returncode, 1)
        self.assertRegex(done.stdout, r"(?m)^Error: GOW_BASE_URL")

    def test_net18_the_guard_refuses_a_non_loopback_target(self):
        base = {"GOW_BASE_URL": "http://127.0.0.1:1/en-us", "GOW_USERS_FILE": "/tmp/gow/fake-accounts",
                "GOW_DASHBOARD_PORT": "",
                "GOW_EVENTS_FILE": os.path.join(tempfile.gettempdir(), "gow-dash-x", "events.jsonl")}
        assert_safe_dashboard_env({**base, "GOW_INTERNET_CHECK_URL": "http://127.0.0.1:9/check"})
        for url in (network.DEFAULT_INTERNET_URL, "http://localhost:9/", "http://127.0.0.1.evil.test/", "", "x"):
            with self.subTest(url):
                with self.assertRaises(UnsafeRun):
                    assert_safe_dashboard_env({**base, "GOW_INTERNET_CHECK_URL": url})


class Loopback(Net):
    def test_net18_a_full_flow_stays_on_loopback(self):
        stub = Stub(self)
        accounts = {"quick@x.test": Account(), EMAIL: SLOW}
        dash = self.net(stub, accounts=accounts, child_harness=FAST, head_status=200)
        self.wait_network(dash, "ok")
        for mode, head, expected in (("200", 200, "no_internet"), ("204", 500, "site_unreachable"), ("204", 200, "ok")):
            stub.set(mode)
            dash.mock.head_status = head
            self.wait_network(dash, expected, timeout=10)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["totals"] and s["totals"]["claimed"] == 1 and s["slots"], message="the first claim")
        self.assertEqual(dash.stop(), 200)
        dash.wait_state(lambda s: not s["control"]["owned"], timeout=40, message="the run to stop")
        self.assertEqual(dash.audit(), [])

    def test_net18_with_a_mock_and_no_target_no_internet_check_runs(self):
        dash = self.net(None)
        state = self.wait_network(dash, "ok")
        self.assertIsNone(state["internet_ms"])
        self.assertIsInstance(state["site_ms"], int)
        self.assertEqual(dash.audit(), [])

    def test_net19_control_without_the_skip_rule_the_hook_catches_and_blocks_it(self):
        dash = self.net(None, harness={"no_net_skip": True}, expect_audit=True)
        state = self.wait_network(dash, "no_internet", timeout=8)
        found = dash.audit()
        self.assertTrue(found, "the detector has to see the attempt")
        self.assertEqual({line.split()[0] for line in found}, {"socket.getaddrinfo"})
        self.assertEqual({line.split()[1] for line in found}, {"www.gstatic.com"})
        self.assertIn(state["failure"], FAILURES)


class Monitor(unittest.TestCase):
    """The rules, with a fake clock, and no thread running"""

    def monitor(self, interval="1", internet="http://127.0.0.1:9/check"):
        env = {"GOW_BASE_URL": "http://127.0.0.1:9/en-us", "GOW_INTERNET_CHECK_URL": internet,
               "GOW_NET_INTERVAL_SECONDS": interval}
        with unittest_mock.patch.dict(os.environ, env):
            return network.NetworkMonitor(lambda: False)

    def clock(self, value):
        return unittest_mock.patch.object(network, "time", SimpleNamespace(monotonic=lambda: value))

    def test_deviation_rechecks_are_capped_at_the_interval(self):
        short = self.monitor("0.5")
        self.assertEqual((short.internet_recheck, short.site_recheck), (0.5, 2.0))
        normal = self.monitor("30")
        self.assertEqual((normal.internet_recheck, normal.site_recheck), (5, 15))
        self.assertEqual(normal.site_interval, 120)

    def test_deviation_a_pass_goes_stale_after_3_intervals(self):
        monitor = self.monitor()
        result = network.CheckResult()
        self.assertTrue(monitor.is_stale(result, 1, 5, 0))
        result.at, result.passed = 100, True
        self.assertFalse(monitor.is_stale(result, 1, 5, 103))
        self.assertTrue(monitor.is_stale(result, 1, 5, 103.1))

    def test_deviation_a_failure_lasts_3_intervals_plus_the_check_deadline(self):
        monitor = self.monitor()
        result = network.CheckResult()
        result.at, result.passed = 100, False
        limit = 3 + 5 + network.HARD_DEADLINE_EXTRA_SECONDS
        self.assertFalse(monitor.is_stale(result, 1, 5, 100 + limit))
        self.assertTrue(monitor.is_stale(result, 1, 5, 100 + limit + 0.1))

    def test_deviation_a_stale_ok_gets_no_debounce_grace(self):
        monitor = self.monitor()
        result = monitor.internet
        with self.clock(100):
            monitor.record(result, monitor.internet_stale, True, 5, 204, None)
        with self.clock(110):
            recheck = monitor.record(result, monitor.internet_stale, False, None, 200, "http")
        self.assertFalse(recheck)
        self.assertIs(result.passed, False)

    def test_a_fresh_ok_does_get_the_debounce(self):
        monitor = self.monitor()
        result = monitor.internet
        with self.clock(100):
            monitor.record(result, monitor.internet_stale, True, 5, 204, None)
        with self.clock(101):
            self.assertTrue(monitor.record(result, monitor.internet_stale, False, None, 200, "http"))
        self.assertIs(result.passed, True)
        with self.clock(102):
            self.assertFalse(monitor.record(result, monitor.internet_stale, False, None, 200, "http"))
        self.assertIs(result.passed, False)

    def test_failure_words(self):
        cases = ((socket.gaierror("x"), "dns"), (TimeoutError(), "timeout"), (socket.timeout(), "timeout"),
                 (ConnectionRefusedError(), "refused"), (ssl.SSLError("x"), "tls"), (ValueError("SECRET"), "error"),
                 (urllib.error.URLError(socket.gaierror("x")), "dns"),
                 (urllib.error.URLError(ConnectionRefusedError()), "refused"),
                 (urllib.error.URLError("text"), "error"))
        for error, word in cases:
            with self.subTest(repr(error)):
                self.assertEqual(network.failure_kind(error), word)

    def test_credentials_are_removed_from_targets(self):
        self.assertEqual(network.without_credentials("http://u:p@h.test:8/x?q=1#frag"), "http://h.test:8/x?q=1")
        self.assertEqual(network.without_credentials("http://u:p@ss@h.test/x"), "http://h.test/x")
        env = {"GOW_BASE_URL": "http://a:b@127.0.0.1:9/en-us", "GOW_INTERNET_CHECK_URL": "http://c:d@127.0.0.1:9/"}
        with unittest_mock.patch.dict(os.environ, env):
            self.assertEqual(network.load_targets(), ("http://127.0.0.1:9/", "http://127.0.0.1:9/en-us/login"))

    def test_the_default_target_is_used_only_with_no_overrides(self):
        with unittest_mock.patch.dict(os.environ):
            os.environ.pop("GOW_BASE_URL", None)
            os.environ.pop("GOW_INTERNET_CHECK_URL", None)
            self.assertEqual(network.load_targets(), (network.DEFAULT_INTERNET_URL, f"{network.DEFAULT_BASE_URL}/login"))
            os.environ["GOW_BASE_URL"] = "http://127.0.0.1:9/en-us"
            self.assertEqual(network.load_targets()[0], None)

    def test_interval_override(self):
        for value, expected in (("0.5", 0.5), ("0", 30), ("-1", 30), ("abc", 30), ("", 30)):
            with self.subTest(value):
                with unittest_mock.patch.dict(os.environ, {"GOW_NET_INTERVAL_SECONDS": value}):
                    self.assertEqual(network.load_interval(), expected)

    def test_a_late_result_is_dropped(self):
        monitor = self.monitor()
        release = threading.Event()

        def slow(url, method, timeout):
            release.wait(5)
            return 204, None

        with unittest_mock.patch.object(network, "probe", slow), \
                unittest_mock.patch.object(network, "HARD_DEADLINE_EXTRA_SECONDS", 0.05):
            self.assertEqual(monitor.bounded_probe("u", "GET", 0.05), (None, "timeout", None))
            self.assertEqual(monitor.abandoned, 1)
            release.set()
            wait_until(lambda: monitor.abandoned == 0, 2, "the abandoned check to end")


class Page(PageCase):
    def net_page(self, stub=None, interval=0.5, accounts=None, harness=None, constants=None, **kwargs):
        config = {**(harness or {}), "net_constants": constants or {}}
        dash = self.dash(accounts or {}, stub=stub, net_interval=interval, harness=config, **kwargs)
        return dash, self.page(dash)

    def posts(self, page):
        return [url for url in page.seen["requests"] if url.endswith(("/api/run", "/api/retry", "/api/stop"))]

    def network_text(self, page):
        return page.inner_text("#network")

    def retry_fixture(self, dash):
        run = "20261101T000000Z-1"
        write_events(dash.events_file, [run_start(run, 1000.0, total=1), account_end(run, 1001.0, 1, claimed=0, success=False),
                                        event("run_end", run, 1002.0, status="finished", dropped=0)])

    def test_net1_indicator(self):
        dash, page = self.net_page(Stub(self))
        wait_online(page)
        text = self.network_text(page)
        self.assertRegex(text, r"^Online · game site OK · \d+ms · checked \d+s ago$")

    def test_net2_net3_net4_indicator_text(self):
        for mode, head, expected in (("200", 200, r"^No internet · checked \d+s ago$"),
                                     ("204", 503, r"^Internet OK, game site unreachable · checked \d+s ago$")):
            with self.subTest(expected):
                stub = Stub(self, mode)
                dash = self.dash(stub=stub, net_interval=0.5)
                dash.mock.head_status = head
                page = self.page(dash)
                wait_until(lambda: "checked" in self.network_text(page), 6, "the indicator")
                self.assertRegex(self.network_text(page), expected)

    def test_net4_indicator_while_checking_is_never_online(self):
        dash = self.dash(stub=Stub(self, "hang"), net_interval=0.5)
        page = self.page(dash)
        for _ in range(6):
            self.assertEqual(self.network_text(page), "Checking connection…")
            page.wait_for_timeout(250)

    def test_net6_changes_appear_with_no_reload(self):
        stub = Stub(self)
        dash, page = self.net_page(stub)
        wait_online(page)
        internet = 2 * 0.5 + 2.5
        site = 2 * (4 * 0.5) + 2.5      # the same rule, on the site's own interval
        for mode, head, expected, limit in (("200", 200, "No internet", internet), ("204", 200, "Online", internet),
                                            ("204", 503, "Internet OK, game site unreachable", site),
                                            ("204", 200, "Online", site)):
            with self.subTest(expected):
                stub.set(mode)
                dash.mock.head_status = head
                self.wait_text(page, "#network", expected, limit)
        self.assertEqual(page.get_attribute("#network-dot", "aria-hidden"), "true")
        self.assertEqual(page.get_attribute("#network", "role"), "status")

    def test_net9_an_open_page_recovers_after_a_restart(self):
        stub = Stub(self)
        dash, page = self.net_page(stub)
        wait_online(page)
        port = dash.port
        dash.kill_server()
        dash.requested_port = port
        dash.start()
        wait_until(lambda: page.get_attribute('meta[name="gow-token"]', "content") == dash.token, 8, "the reload")
        wait_online(page)

    def test_net20_unknown_when_the_server_is_down_and_in_view_at_both_widths(self):
        stub = Stub(self)
        dash, page = self.net_page(stub)
        wait_online(page)
        dash.kill_server()
        self.wait_text(page, "#network", "Connection status unknown", 4)
        self.assertNotIn("Online", self.network_text(page))
        self.wait_text(page, "#alert", "Dashboard not responding", 4)

    def test_net20_sticky_strip_above_the_header_with_no_side_scroll(self):
        run = "20261101T000000Z-1"
        dash = self.dash()
        write_events(dash.events_file, [run_start(run, 1000.0, total=60)] + [account_end(run, 1001.0, i) for i in range(1, 61)]
                     + [event("run_end", run, 1002.0, status="finished", dropped=0)])
        for width in (1360, 390):
            with self.subTest(width):
                page = self.page(dash, viewport={"width": width, "height": 600})
                wait_online(page)
                page.wait_for_timeout(300)
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                page.wait_for_timeout(300)
                self.assertGreater(page.evaluate("window.scrollY"), 100, "the page has to scroll to prove anything")
                box = page.locator("#network").bounding_box()
                self.assertGreaterEqual(box["y"], 0)
                self.assertLess(box["y"], 600)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                order = page.evaluate("""() => {
                    const strip = document.querySelector('.net-strip');
                    const header = document.querySelector('header');
                    return Boolean(strip.compareDocumentPosition(header) & Node.DOCUMENT_POSITION_FOLLOWING);
                }""")
                self.assertTrue(order, "the strip sits above the header")

    def test_net20_static_files_and_ui1(self):
        for name in ("index.html", "app.js", "app.css"):
            with open(os.path.join(PROJECT, "dashboard", "static", name), encoding="utf-8") as f:
                text = f.read()
            with self.subTest(name):
                self.assertNotIn("navigator.onLine", text)
                self.assertNotIn("onLine", text)
        dash, page = self.net_page(Stub(self))
        wait_online(page)
        origin = f"http://127.0.0.1:{dash.port}/"
        self.assertEqual([url for url in page.seen["requests"] if not url.startswith(origin)], [])
        self.assertEqual([m for m in page.seen["console"] if m[0] in ("error", "warning")], [])
        self.assertEqual(page.seen["errors"], [])

    def gate_shows(self, page, text):
        self.assertTrue(page.is_visible("#gate"))
        self.assertEqual(page.get_attribute("#gate", "role"), "alert")
        self.assertIn(text, page.inner_text("#gate-text"))

    def test_net13_start_gate_in_each_state(self):
        for mode, head, wording, indicator in (("hang", 200, "Still checking", "Checking"),
                                               ("200", 200, "No internet", "No internet"),
                                               ("204", 503, "Game site unreachable", "game site unreachable")):
            with self.subTest(wording):
                stub = Stub(self, mode)
                dash = self.dash({EMAIL: Account(vault="cooldown")}, stub=stub, net_interval=0.5, harness={},
                                 child_harness=FAST)
                dash.mock.head_status = head
                page = self.page(dash)
                self.wait_text(page, "#network", indicator, 6)
                page.click('[data-concurrency="2"]')
                page.click("#primary")
                self.gate_shows(page, wording)
                self.assertEqual(self.posts(page), [])
                self.assertEqual(page.inner_text("#gate-go"), "Start anyway")

                page.click("#gate-cancel")
                self.assertFalse(page.is_visible("#gate"))
                self.assertEqual(self.posts(page), [])
                self.assertEqual(dash.spawns(), [])

                page.click("#primary")
                page.click("#gate-go")
                wait_until(lambda: dash.spawns(), 5, "the spawn")
                self.assertEqual(len(self.posts(page)), 1)
                self.assertEqual(dash.spawns()[0]["argv"][-2:], ["--concurrency", "2"])
                page.wait_for_timeout(1200)
                self.assertEqual(len(dash.spawns()), 1)

    def test_net13_retry_gate(self):
        stub = Stub(self, "200")
        dash = self.dash({EMAIL: Account(vault="cooldown")}, stub=stub, net_interval=0.5, harness={}, child_harness=FAST)
        self.retry_fixture(dash)
        page = self.page(dash)
        self.wait_text(page, "#network", "No internet")
        self.wait_text(page, "#retry", "Retry 1 failed")
        page.click("#retry")
        self.gate_shows(page, "No internet")
        self.assertEqual(page.inner_text("#gate-go"), "Retry anyway")
        self.assertEqual(self.posts(page), [])
        page.click("#gate-go")
        wait_until(lambda: dash.spawns(), 5, "the retry")
        self.assertEqual(dash.spawns()[0]["argv"][-2:], ["--lines", "1"])
        self.assertEqual(len(self.posts(page)), 1)

    def test_net13_ok_needs_one_click(self):
        dash = self.dash({EMAIL: Account(vault="cooldown")}, stub=Stub(self), net_interval=0.5, harness={}, child_harness=FAST)
        page = self.page(dash)
        wait_online(page)
        page.click("#primary")
        self.assertFalse(page.is_visible("#gate"))
        wait_until(lambda: dash.spawns(), 5, "the spawn")

    def test_net13_the_warning_closes_on_ok_and_starts_nothing(self):
        stub = Stub(self, "200")
        dash = self.dash({EMAIL: Account(vault="cooldown")}, stub=stub, net_interval=0.5, harness={})
        page = self.page(dash)
        self.wait_text(page, "#network", "No internet")
        page.click("#primary")
        self.gate_shows(page, "No internet")
        stub.set("204")
        wait_until(lambda: not page.is_visible("#gate"), 8, "the warning to close")
        wait_online(page)
        page.wait_for_timeout(1200)
        self.assertEqual(self.posts(page), [])
        self.assertEqual(dash.spawns(), [])

    def test_deviation_the_warning_closes_if_a_run_becomes_live(self):
        stub = Stub(self, "200")
        dash = self.dash({EMAIL: SLOW}, stub=stub, net_interval=0.5, harness={})
        page = self.page(dash)
        self.wait_text(page, "#network", "No internet")
        page.click("#primary")
        self.gate_shows(page, "No internet")
        self.terminal_run(dash)
        wait_until(lambda: not page.is_visible("#gate"), 15, "the warning to close")
        self.assertEqual(self.posts(page), [])
        self.assertEqual(dash.spawns(), [])

    def test_net13_stop_is_never_gated(self):
        stub = Stub(self)
        accounts = {EMAIL: SLOW}
        dash = self.dash(accounts, stub=stub, net_interval=0.5, harness={}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        page = self.page(dash)
        self.wait_primary(page, "Stop")
        stub.set("200")
        self.wait_text(page, "#network", "No internet", 6)
        page.click("#primary")
        self.assertFalse(page.is_visible("#gate"))
        wait_until(lambda: dash.signals(), 5, "the stop signal")

    def live_run(self, stub):
        dash = self.dash({EMAIL: SLOW}, stub=stub, net_interval=0.5, harness={})
        self.terminal_run(dash)
        dash.wait_state(lambda s: s["live"], message="the terminal run")
        return dash

    def test_net14_banner_for_a_terminal_run(self):
        stub = Stub(self)
        dash = self.live_run(stub)
        page = self.page(dash)
        wait_online(page)
        self.assertFalse(page.is_visible("#network-banner"))
        self.assertEqual(page.get_attribute("#network-banner", "role"), "alert")

        for mode, head, start in (("200", 200, "No internet."), ("204", 503, "Game site unreachable.")):
            with self.subTest(start):
                stub.set(mode)
                dash.mock.head_status = head
                self.wait_text(page, "#network-banner", start, 10)
                text = page.inner_text("#network-banner")
                self.assertIn("Accounts may be failing", text)
                self.assertIn("The run has not been paused", text)
                stub.set("204")
                dash.mock.head_status = 200
                wait_until(lambda: not page.is_visible("#network-banner"), 10, "the banner to clear")
        self.assertEqual(dash.signals(), [])
        self.assertTrue(dash.state()["live"])

    def test_net14_hidden_while_checking_and_with_no_live_run(self):
        stub = Stub(self, "hang")
        dash = self.live_run(stub)
        page = self.page(dash)
        self.wait_text(page, "#network", "Checking")
        page.wait_for_timeout(1500)
        self.assertFalse(page.is_visible("#network-banner"))

        idle_dash = self.dash(stub=Stub(self, "200"), net_interval=0.5)
        idle = self.page(idle_dash)
        self.wait_text(idle, "#network", "No internet")
        idle.wait_for_timeout(1200)
        self.assertFalse(idle.is_visible("#network-banner"))

    def test_net14_banner_stays_beside_another_notice(self):
        run = "20261101T000000Z-1"
        dash = self.dash(stub=Stub(self, "200"), net_interval=0.5)
        lock = f"{dash.events_file}.locks/{run}.lock"
        os.makedirs(os.path.dirname(lock), mode=0o700)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        write_events(dash.events_file, [run_start(run, time.time() - 300)])
        page = self.page(dash)
        self.wait_text(page, "#network-banner", "No internet.")
        self.wait_text(page, "#alert", "No activity for")
        self.assertTrue(page.is_visible("#network-banner"))
        self.assertTrue(page.is_visible("#alert"))

    def test_net12_start_anyway_gives_the_same_result(self):
        accounts = {"alpha@x.test": Account(), "bravo@x.test": Account(vault="cooldown"),
                    "charlie@x.test": Account(password="other")}
        dash = self.dash(accounts, stub=Stub(self, "200"), net_interval=0.5, harness={}, child_harness=FAST)
        page = self.page(dash)
        self.wait_text(page, "#network", "No internet")
        page.click('[data-concurrency="1"]')
        page.click("#primary")
        page.click("#gate-go")
        state = dash.wait_state(lambda s: s["status"] == "finished" and not s["control"]["owned"], timeout=60, message="the run")
        self.assertEqual(sorted(dash.mock.ledger_pairs()), [("alpha@x.test", "only")])
        self.assertEqual((state["totals"]["claimed"], state["totals"]["already"], state["totals"]["failed"]), (1, 1, 1))
        self.assertEqual(state["totals"]["jewels"], 75)
        self.assertEqual(dash.child_exit(), "1")


if __name__ == "__main__":
    unittest.main()
