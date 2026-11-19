"""Dashboard D8: what the server accepts, serves and refuses. No request here starts a real run
except where a test says so, and every server is on loopback over a mock."""

import ipaddress
import os
import re
import socket
import stat
import subprocess
import tempfile
import unittest

from dash_support import (
    SERVER,
    DashTestCase,
    account_end,
    assert_safe_dashboard_env,
    event,
    run_start,
    script_env,
    write_events,
)
from gow_support import PROJECT, PYTHON, Account, UnsafeRun, users_text, wait_until

EMAIL = "request@x.test"
SLOW = {EMAIL: Account(vault_delay_ms=30000)}


def source(name):
    with open(os.path.join(PROJECT, "dashboard", name), encoding="utf-8") as f:
        return f.read()


class Guards(unittest.TestCase):
    SAFE = {"GOW_BASE_URL": "http://127.0.0.1:1/en-us", "GOW_USERS_FILE": "/tmp/gow/fake-accounts",
            "GOW_DASHBOARD_PORT": ""}

    def test_a_dashboard_run_needs_a_temp_event_file(self):
        good = os.path.join(tempfile.gettempdir(), "gow-dash-x", "events.jsonl")
        assert_safe_dashboard_env({**self.SAFE, "GOW_EVENTS_FILE": good})
        for path in (None, "", os.path.join(PROJECT, "dashboard", "data", "events.jsonl"), "/var/lib/events.jsonl"):
            with self.subTest(path):
                env = dict(self.SAFE)
                if path is not None:
                    env["GOW_EVENTS_FILE"] = path
                with self.assertRaises(UnsafeRun):
                    assert_safe_dashboard_env(env)


class Docs(unittest.TestCase):
    def test_doc1_readme_has_the_dashboard_section_and_lines(self):
        with open(os.path.join(PROJECT, "README.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertRegex(text, r"(?m)^## .*Dashboard")
        section = text[text.index("Dashboard\n"):]
        self.assertIn("dashboard/server.py", section)
        self.assertIn("dashboard/data/events.jsonl", section)
        self.assertIn("Ctrl+C", section)
        self.assertRegex(text, r"claim_all_characters\.py --lines \d")

    def test_doc1_readme_explains_the_status_check(self):
        with open(os.path.join(PROJECT, "README.md"), encoding="utf-8") as f:
            text = f.read()
        section = text[text.index("Dashboard\n"):]
        self.assertIn("generate_204", section)
        self.assertIn("GOW_INTERNET_CHECK_URL", section)
        self.assertRegex(section, r"(?i)every 30")
        self.assertRegex(section, r"(?i)never pauses?")


class FullEmailEndpoint(DashTestCase):
    ADDRESS = "victim.person@example.test"
    RUN = "20261101T000000Z-4242"

    def fixture(self, dash):
        write_events(dash.events_file, [
            run_start(self.RUN, 1000.0, total=1), account_end(self.RUN, 1001.0, 1, label=self.ADDRESS),
            event("run_end", self.RUN, 1002.0, status="finished", dropped=0)])

    def state_with(self, dash, *headers):
        return dash.request("GET", "/api/state", headers=[("Host", dash.host()), *headers])

    def test_em1_the_fixture_email_is_served(self):
        dash = self.dash()
        self.fixture(dash)
        self.assertIn(self.ADDRESS.encode(), dash.get("/api/state")[2])

    def test_em6_a_forged_label_is_dropped_and_good_lines_stay(self):
        dash = self.dash()
        self.fixture(dash)
        write_events(dash.events_file, [account_end(self.RUN, 1003.0, 2, label="<img src=forged>"),
                                        account_end(self.RUN, 1004.0, 3, label="forged password")])
        data = dash.get("/api/state")[2]
        self.assertNotIn(b"forged", data)
        self.assertIn(self.ADDRESS.encode(), data)

    def test_ep1_a_bad_host_gets_no_account_data(self):
        dash = self.dash()
        self.fixture(dash)
        port = dash.port
        for host in ("evil.test", f"evil.test:{port}", f"127.0.0.1.evil.test:{port}", "localhost:1"):
            with self.subTest(host):
                status, _, data = dash.get("/api/state", host=host)
                self.assertEqual(status, 403)
                self.assertNotIn(b"victim", data)
        status, _, data = dash.request("GET", "/api/state")
        self.assertEqual(status, 403)
        self.assertNotIn(b"victim", data)

    def test_ep3_cross_site_requests_are_refused(self):
        dash = self.dash()
        self.fixture(dash)
        for value in ("cross-site", "same-site"):
            with self.subTest(value):
                status, _, data = self.state_with(dash, ("Sec-Fetch-Site", value))
                self.assertEqual(status, 403)
                self.assertNotIn(b"victim", data)
        for headers in ([("Sec-Fetch-Site", "same-origin")], [("Sec-Fetch-Site", "none")], []):
            with self.subTest(headers=headers):
                status, _, data = self.state_with(dash, *headers)
                self.assertEqual(status, 200)
                self.assertIn(self.ADDRESS.encode(), data)
        status, _, data = self.state_with(dash, ("Origin", "https://evil.test"))
        self.assertEqual(status, 403)
        self.assertNotIn(b"victim", data)

    def test_ep3_odd_header_values_are_refused(self):
        dash = self.dash()
        self.fixture(dash)
        for headers in ([("Sec-Fetch-Site", "SAME-ORIGIN")], [("Sec-Fetch-Site", "")],
                        [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")],
                        [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")]):
            with self.subTest(headers=headers):
                status, _, data = self.state_with(dash, *headers)
                self.assertEqual(status, 403)
                self.assertNotIn(b"victim", data)

    def test_ep5_control_without_the_check_the_email_is_sent(self):
        dash = self.dash(harness={"no_fetch_site_check": True})
        self.fixture(dash)
        status, _, data = self.state_with(dash, ("Sec-Fetch-Site", "cross-site"))
        self.assertEqual(status, 200)
        self.assertIn(self.ADDRESS.encode(), data)

    def test_ep6_only_the_state_route_serves_it(self):
        dash = self.dash()
        self.fixture(dash)
        self.assertEqual(dash.get("/api/state")[0], 200)
        for path in ("/", "/app.js", "/app.css"):
            with self.subTest(path):
                status, _, data = dash.get(path)
                self.assertEqual(status, 200)
                self.assertNotIn(b"victim", data)
        self.assertEqual(dash.get("/api/state?x=1")[0], 404)

    def test_other_routes_ignore_the_fetch_site_header(self):
        """A2 is for /api/state only: the page must still open from a link on another site"""
        dash = self.dash()
        status = dash.request("GET", "/", headers=[("Host", dash.host()), ("Sec-Fetch-Site", "cross-site")])[0]
        self.assertEqual(status, 200)


class Listening(DashTestCase):
    def test_req1_loopback_only(self):
        dash = self.dash()
        done = subprocess.run(["lsof", "-nP", "-a", "-p", str(dash.process.pid), "-iTCP", "-sTCP:LISTEN"],
                              capture_output=True, text=True)
        self.assertIn(f"127.0.0.1:{dash.port}", done.stdout)
        self.assertNotRegex(done.stdout, r"\*:" + str(dash.port))

    def test_req1_refused_on_the_machines_other_address(self):
        dash = self.dash()
        found = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
        addresses = [a for a in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", found)
                     if not ipaddress.ip_address(a).is_loopback]
        if not addresses:
            self.skipTest("this machine has no other interface address")
        for address in addresses:
            with self.subTest(address):
                probe = socket.socket()
                probe.settimeout(2)
                self.addCleanup(probe.close)
                self.assertNotEqual(probe.connect_ex((address, dash.port)), 0)

    def test_req1_host_flag_is_an_error(self):
        mock = self.make_mock()
        env = script_env(mock, "/tmp/gow-dash-unused")
        env["GOW_EVENTS_FILE"] = "/tmp/gow-dash-unused/events.jsonl"
        done = subprocess.run([PYTHON, "-B", SERVER, "--host", "0.0.0.0", "--port", "0"], cwd=PROJECT, env=env,
                              capture_output=True, text=True, timeout=20)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--host", done.stderr)


class HostHeader(DashTestCase):
    def test_req2(self):
        dash = self.dash(harness={})
        port = dash.port
        for host in ("evil.test", f"evil.test:{port}", f"127.0.0.1.evil.test:{port}", "localhost:1", "127.0.0.1"):
            with self.subTest(host):
                self.assertEqual(dash.get("/api/state", host=host)[0], 403)
                self.assertEqual(dash.post("/api/run", {"concurrency": 3}, host=host)[0], 403)

        self.assertEqual(dash.request("GET", "/api/state")[0], 403)
        twice = [("Host", dash.host()), ("Host", "evil.test")]
        self.assertEqual(dash.request("GET", "/api/state", headers=twice)[0], 403)
        self.assertEqual(dash.spawns(), [])

        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            with self.subTest(host):
                self.assertEqual(dash.get("/api/state", host=host)[0], 200)


class Origin(DashTestCase):
    def test_req3(self):
        dash = self.dash(harness={})
        for origin in ("https://evil.test", "null", "http://127.0.0.1:1", None):
            with self.subTest(origin):
                self.assertEqual(dash.post("/api/run", {"concurrency": 3}, origin=origin)[0], 403)
        self.assertEqual(dash.spawns(), [])


class Token(DashTestCase):
    def test_req4(self):
        dash = self.dash(harness={})
        previous = dash.token
        dash.kill_server()
        dash.start()
        self.assertNotEqual(previous, dash.token)
        for token in (None, "wrong", previous, ""):
            with self.subTest(token=bool(token)):
                self.assertEqual(dash.post("/api/run", {"concurrency": 3}, token=token)[0], 403)
        self.assertEqual(dash.spawns(), [])


class Bodies(DashTestCase):
    def test_req5(self):
        dash = self.dash(harness={})
        self.assertEqual(dash.post("/api/run", {"concurrency": 3}, content_type="text/plain")[0], 415)
        self.assertEqual(dash.post("/api/run", {"concurrency": 3}, content_type=None)[0], 415)
        self.assertEqual(dash.post("/api/run", raw=b'{"concurrency":3,"x":"' + b"y" * 1100 + b'"}')[0], 413)
        for name, raw in (("invalid", b"{"), ("array", b"[]"), ("string", b'"x"'), ("number", b"3"),
                          ("nothing", b"{}"), ("unknown key", b'{"concurrency":3,"x":1}'), ("only other", b'{"x":1}')):
            with self.subTest(name):
                self.assertEqual(dash.post("/api/run", raw=raw)[0], 400)
        for value in ("0", "4", "6", "10", '"3"', "3.0", "true", "null", "[3]", "1e0", "-1"):
            with self.subTest(value):
                self.assertEqual(dash.post("/api/run", raw=f'{{"concurrency":{value}}}'.encode())[0], 400)
                self.assertEqual(dash.post("/api/retry", raw=f'{{"concurrency":{value}}}'.encode())[0], 400)
        self.assertEqual(dash.post("/api/stop", {"x": 1})[0], 400)
        self.assertEqual(dash.spawns(), [])

    def test_req6_no_cors_and_the_wrong_methods(self):
        dash = self.dash(harness={})
        self.assertEqual(dash.request("OPTIONS", "/api/run", headers=[
            ("Host", dash.host()), ("Origin", "https://evil.test"), ("Access-Control-Request-Method", "POST")])[0], 405)
        before = dash.state()
        for path in ("/api/run", "/api/retry", "/api/stop"):
            with self.subTest(path):
                self.assertEqual(dash.get(path)[0], 405)
        self.assertEqual(dash.spawns(), [])
        self.assertEqual(dash.state()["status"], before["status"])

    def test_req7_every_response_has_the_security_headers(self):
        dash = self.dash()
        host = [("Host", dash.host())]
        responses = [dash.get(path) for path in ("/", "/app.js", "/app.css", "/api/state", "/nope", "/api/run")]
        responses += [dash.get("/api/state", host="evil.test"), dash.request("GET", "/api/state"),
                      dash.request("OPTIONS", "/", headers=host), dash.request("PUT", "/", body="x", headers=host),
                      dash.post("/api/stop", {}), dash.post("/api/run", {"concurrency": 3}, token="wrong"),
                      dash.post("/api/run", raw=b"{"), dash.post("/nope", {})]
        for status, headers, _ in responses:
            with self.subTest(status=status):
                self.assert_headers(headers)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Content-Security-Policy"],
                                 "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                 "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    def test_req8_child_argv(self):
        dash = self.dash(SLOW, harness={})
        self.assertEqual(dash.run(3), 200)
        wait_until(lambda: dash.spawns(), 10, "the spawn")
        (spawn,) = dash.spawns()
        self.assertEqual(os.path.realpath(spawn["argv"][0]), os.path.realpath(PYTHON))
        self.assertEqual(spawn["argv"][1:], [os.path.join(PROJECT, "claim_all_characters.py"), "--concurrency", "3"])
        self.assertIs(spawn["shell"], False)
        self.assertTrue(spawn["new_session"])
        self.assertTrue(spawn["stdout_is_file"])
        self.assertEqual(spawn["source"], "dashboard")
        dash.wait_state(lambda s: s["run"] is not None, message="the run to appear")
        self.assertEqual(dash.stop(), 200)
        dash.wait_state(lambda s: not s["control"]["owned"], timeout=30, message="the run to stop")


class Controls(DashTestCase):
    """REQ9: each attack from REQ2-4 succeeds with its one protection removed"""

    def test_host_check_removed(self):
        dash = self.dash(SLOW, harness={"no_host_check": True}, child_harness={})
        self.assertEqual(dash.get("/api/state", host="evil.test")[0], 200)
        self.assertEqual(dash.post("/api/run", {"concurrency": 3}, host="evil.test")[0], 200)
        self.assertEqual(len(dash.spawns()), 1)

    def test_origin_check_removed(self):
        dash = self.dash(SLOW, harness={"no_origin_check": True}, child_harness={})
        self.assertEqual(dash.post("/api/run", {"concurrency": 3}, origin="https://evil.test")[0], 200)
        self.assertEqual(len(dash.spawns()), 1)

    def test_token_check_removed(self):
        dash = self.dash(SLOW, harness={"no_token_check": True}, child_harness={})
        self.assertEqual(dash.post("/api/run", {"concurrency": 3}, token="wrong")[0], 200)
        self.assertEqual(len(dash.spawns()), 1)


class Files(DashTestCase):
    def test_sec5_no_path_reaches_a_file(self):
        dash = self.dash()
        with open(dash.users_file, "w") as f:
            f.write("marker@x.test:marker-password\n")
        for path in ("/users.txt", "/../users.txt", "/%2e%2e/users.txt", "/dashboard/data/events.jsonl",
                     "/data/last-run.log", "/last-run.log", "/.env.example", "/app.js/../../users.txt",
                     "/static/../server.py", "/app.js?x=1", "/app.js/", "/server.py"):
            with self.subTest(path):
                status, _, data = dash.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"marker", data)
                self.assertLess(len(data), 100)

    def test_sec4_token_only_in_the_page(self):
        dash = self.dash()
        token = dash.token.encode()
        page = dash.get("/")
        self.assertIn(token, page[2])
        self.assertNotIn(dash.token, str(page[1]))
        for path in ("/api/state", "/app.js", "/app.css", "/nope"):
            with self.subTest(path):
                status, headers, data = dash.get(path)
                self.assertNotIn(token, data)
                self.assertNotIn(dash.token, str(headers))
        for refused in (dash.post("/api/stop", {}), dash.post("/api/run", {"concurrency": 3}, token="wrong")):
            self.assertNotIn(token, refused[2])
            self.assertNotIn(dash.token, str(refused[1]))
        self.assertNotIn(dash.token, dash.output())

    def test_sec6_static_check(self):
        for name in ("server.py", "store.py"):
            text = source(name)
            for banned in ("GOW_USERS_FILE", "users.txt", "claim_common"):
                with self.subTest(name, banned=banned):
                    self.assertNotIn(banned, text)

    def test_sec6_works_with_the_users_file_unreadable(self):
        dash = self.dash()
        os.chmod(dash.users_file, 0)
        self.addCleanup(os.chmod, dash.users_file, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(dash.state()["status"], "idle")
        self.assertEqual(dash.get("/")[0], 200)

    def test_sec7_forged_lines_stay_out_of_the_state(self):
        dash = self.dash()
        run = "20261101T000000Z-4242"
        write_events(dash.events_file, [
            run_start(run, 1000.0), event("account_start", run, 1001.0, line=1, label="<img src=victim>"),
            event("stage", run, 1002.0, line=1, stage="<img src=victim>"),
        ])
        self.assertEqual(dash.get("/api/state")[0], 200)
        self.assertNotIn(b"victim", dash.get("/api/state")[2])

    def test_deviation_run_log_sits_beside_the_event_file(self):
        dash = self.dash({EMAIL: Account(vault="cooldown")})
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["status"] == "finished", message="the run to finish")
        path = os.path.join(os.path.dirname(dash.events_file), "last-run.log")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIn("MULTI-CHARACTER BATCH SUMMARY", dash.log())


if __name__ == "__main__":
    unittest.main()
