"""Dashboard D8: one run at a time, Stop, Retry and crashes, against real mock runs."""

import fcntl
import os
import signal
import subprocess
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock as unittest_mock

from dash_support import (
    SIGINT,
    SIGKILL,
    SIGTERM,
    DashTestCase,
    read_events,
    run_start,
    write_events,
)
from gow_support import PYTHON, Account, command_of, descendants, kill_all, still_running, users_text, wait_until

from dashboard import events, server

FAST = {"default_stagger": 0.1}
EMAIL = "run@x.test"
SLOW = Account(vault_delay_ms=25000)
SCRIPT = "claim_all_characters.py"


def lock_for(dash, run):
    """Hold a run's lock from here, as a live process would"""
    path = events.lock_path(dash.events_file, run)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class OneRun(DashTestCase):
    def test_run1_second_start_and_retry_are_refused_while_live(self):
        dash = self.dash({EMAIL: SLOW}, harness={}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["live"], message="the run to go live")
        self.assertEqual(dash.run(), 409)
        self.assertEqual(dash.retry(), 409)
        self.assertEqual(len(dash.spawns()), 1)

    def test_run1_five_concurrent_starts_make_one_child(self):
        dash = self.dash({EMAIL: SLOW}, harness={}, child_harness=FAST)
        with ThreadPoolExecutor(5) as pool:
            statuses = list(pool.map(lambda _: dash.run(), range(5)))
        self.assertEqual(sorted(statuses), [200, 409, 409, 409, 409])
        self.assertEqual(len(dash.spawns()), 1)

    def test_run2_a_terminal_run_blocks_start_and_retry(self):
        dash = self.dash({EMAIL: SLOW}, harness={})
        self.terminal_run(dash)
        state = dash.wait_state(lambda s: s["live"], message="the terminal run")
        self.assertEqual(state["run"]["source"], "terminal")
        self.assertEqual(state["status"], "running")
        self.assertEqual(dash.run(), 409)
        self.assertEqual(dash.retry(), 409)
        self.assertEqual(dash.spawns(), [])

    def test_run3_start_works_after_a_run_ends(self):
        dash = self.dash({EMAIL: Account(vault="cooldown")}, harness={}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["status"] == "finished" and not s["control"]["owned"], message="the first run")
        self.assertEqual(dash.run(), 200)
        self.assertEqual(len(dash.spawns()), 2)

    def test_run3_start_works_after_a_crash(self):
        dash = self.dash({EMAIL: SLOW}, harness={}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["live"], message="the run to go live")
        os.kill(dash.child_pid(), signal.SIGKILL)
        dash.wait_state(lambda s: s["status"] == "crashed" and not s["control"]["owned"], timeout=10, message="crash")
        self.assertEqual(dash.run(), 200)

    def test_run4_a_terminal_run_beside_a_dashboard_run(self):
        accounts = {EMAIL: Account(vault_delay_ms=12000)}
        dash = self.dash(accounts, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["live"], message="the dashboard run")
        time.sleep(1.1)         # run ids have one-second resolution
        process = self.terminal_run(dash)
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, stdout[-1000:])
        self.assertIn("BATCH SUMMARY", stdout)
        state = dash.wait_state(lambda s: not s["live"] and len(s["recent_runs"]) == 2, message="both runs")
        self.assertEqual(sorted(run["source"] for run in state["recent_runs"]), ["dashboard", "terminal"])

    def test_run5_control_without_the_live_run_check(self):
        dash = self.dash({EMAIL: SLOW}, harness={"no_live_check": True}, child_harness=FAST)
        self.terminal_run(dash)
        dash.wait_state(lambda s: s["live"], message="the terminal run")
        self.assertEqual(dash.run(), 200)
        self.assertEqual(len(dash.spawns()), 1)


class Stop(DashTestCase):
    ACCOUNTS = {"quick@x.test": Account(), EMAIL: SLOW}

    def stopping_run(self, **kwargs):
        dash = self.dash(self.ACCOUNTS, harness=kwargs.pop("harness", {}), child_harness=FAST, **kwargs)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["totals"] and s["totals"]["claimed"] == 1 and s["slots"], message="the first claim")
        return dash

    def assert_stopped(self, dash):
        state = dash.wait_state(lambda s: not s["control"]["owned"], timeout=40, message="the run to end")
        self.assertEqual(dash.child_exit(), "1")
        self.assertIn("Interrupted by user", dash.log())
        self.assertNotIn("SUMMARY", dash.log())
        ended = [e for e in dash.recorded() if e["event"] == "run_end"]
        self.assertEqual([e["status"] for e in ended], ["interrupted"])
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["totals"]["jewels"], 75)
        self.assertEqual(state["totals"]["claimed"], 1)
        return state

    def test_stop1_stop2_one_sigint_and_nothing_left_behind(self):
        dash = self.stopping_run()
        child = dash.child_pid()
        family = descendants(child)
        self.assertGreaterEqual(len(family), 2)
        self.assertEqual(dash.stop(), 200)
        self.assert_stopped(dash)
        self.assertEqual([(s["to"], s["number"]) for s in dash.signals()], [("pid", SIGINT)])
        self.assertEqual(dash.signals()[0]["pid"], child)
        wait_until(lambda: not still_running(family), 5, "the driver and browser to exit")

    def test_stop4_a_second_stop_sends_nothing_more(self):
        dash = self.stopping_run()
        self.assertEqual(dash.stop(), 200)
        self.assertEqual(dash.stop(), 200)
        self.assertEqual(dash.stop(), 200)
        self.assert_stopped(dash)
        self.assertEqual(len(dash.signals()), 1)

    def test_stop5_nothing_to_stop(self):
        dash = self.dash(harness={})
        self.assertEqual(dash.stop(), 409)
        self.assertEqual(dash.signals(), [])

    def test_stop5_a_terminal_run_is_not_stopped(self):
        dash = self.dash({EMAIL: SLOW}, harness={})
        process = self.terminal_run(dash)
        dash.wait_state(lambda s: s["live"], message="the terminal run")
        self.assertEqual(dash.stop(), 409)
        self.assertEqual(dash.signals(), [])
        self.assertIsNone(process.poll())

    def test_stop5_a_forged_pid_is_never_signalled(self):
        bystander = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        dash = self.dash(harness={})
        run = f"20261101T000000Z-{bystander.pid}"
        fd = lock_for(dash, run)
        self.addCleanup(os.close, fd)
        write_events(dash.events_file, [run_start(run, time.time())])
        state = dash.wait_state(lambda s: s["live"], message="the forged run")
        self.assertEqual(state["run"]["id"], run)
        self.assertEqual(dash.stop(), 409)
        self.assertEqual(dash.signals(), [])
        time.sleep(0.3)
        self.assertIsNone(bystander.poll())

    def test_stop6_server_launched_with_sigint_ignored(self):
        dash = self.stopping_run(ignore_sigint=True)
        self.assertEqual(dash.stop(), 200)
        self.assert_stopped(dash)

    def test_stop7_control_with_the_sigint_removed(self):
        dash = self.stopping_run(harness={"no_stop_signal": True})
        child = dash.child_pid()
        self.assertEqual(dash.stop(), 200)
        time.sleep(5)
        state = dash.state()
        self.assertEqual(state["status"], "stopping")
        self.assertTrue(still_running([str(child)]))
        self.assertEqual([e for e in dash.recorded() if e["event"] == "run_end"], [])

    def test_stop7_with_the_signal_sent_to_the_process_group(self):
        """Reported either way: this shows what a group signal does to teardown"""
        dash = self.stopping_run(harness={"stop_signal_group": True})
        family = descendants(dash.child_pid())
        self.assertEqual(dash.stop(), 200)
        self.assert_stopped(dash)
        wait_until(lambda: not still_running(family), 5, "the driver and browser to exit")
        self.assertEqual(dash.signals()[0]["number"], SIGINT)

    def test_ev9_a_claim_before_stop_stays_counted(self):
        email = "partial@x.test"
        # c2's switch never answers, which holds the run still between characters
        account = Account(characters=("c1", "c2", "c3"), select_failures=("c2",), select_failure_status=0)
        dash = self.dash({email: account}, child_harness=FAST)
        self.assertEqual(dash.run(1), 200)
        wait_until(lambda: dash.mock.ledger_pairs(), 30, "the first claim")
        time.sleep(1)
        self.assertEqual(dash.stop(), 200)
        state = dash.wait_state(lambda s: not s["control"]["owned"], timeout=40, message="the run to end")
        (end,) = [e for e in dash.recorded() if e["event"] == "account_end"]
        self.assertEqual(end["claimed"], 1)
        self.assertIs(end["interrupted"], True)
        self.assertEqual(state["totals"]["jewels"], 75)
        self.assertEqual(state["recent_runs"][0]["jewels"], 75)


class CleanupGuards(unittest.TestCase):
    """A test once tried to clean up pid 1's children, from a fixture's run id. Never again"""

    def test_the_process_tree_is_never_walked_from_pid_1(self):
        for pid in (0, 1, "1"):
            with self.subTest(pid):
                with self.assertRaises(ValueError):
                    descendants(pid)

    def test_pid_1_is_never_signalled(self):
        with self.assertRaises(ValueError):
            kill_all(["1"])


class FakeStore:
    def snapshot(self):
        return {"live": False, "retry_lines": []}

    def has_run_for_pid(self, pid):
        return True


STUBBORN = """
import signal, sys, time
for number in {signals}:
    signal.signal(number, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(60)
"""


class Escalation(unittest.TestCase):
    """STOP3, on a real child and real signals, with low constants"""

    def run_stubborn(self, ignored):
        control = server.RunControl(FakeStore())
        program = STUBBORN.format(signals=[int(s) for s in ignored])
        child = {}

        def spawn(arguments):
            child["process"] = subprocess.Popen([PYTHON, "-B", "-c", program], stdout=subprocess.PIPE,
                                                start_new_session=True, text=True)
            child["process"].stdout.readline()
            return child["process"]

        control.spawn = spawn
        sent = []
        real_group = control.signal_group

        def signal_group(process, number):
            sent.append(number)
            real_group(process, number)

        control.signal_group = signal_group
        with unittest_mock.patch.object(server, "STOP_GRACE_SECONDS", 0.5), \
                unittest_mock.patch.object(server, "STOP_KILL_SECONDS", 0.5), \
                unittest_mock.patch.object(server, "MONITOR_SECONDS", 0.05):
            control.start(1)
            self.addCleanup(child["process"].kill)
            self.addCleanup(child["process"].stdout.close)
            control.stop()
            self.assertEqual(control.state()["phase"], "stopping")
            control.stop()
            wait_until(lambda: control.state()["phase"] is None, 10, "the state to leave stopping")
        self.assertIsNotNone(child["process"].returncode)
        self.assertFalse(control.state()["owned"])
        return sent, child["process"].returncode

    def test_stop3_sigterm_after_the_grace(self):
        sent, code = self.run_stubborn([signal.SIGINT])
        self.assertEqual(sent, [signal.SIGTERM])
        self.assertEqual(code, -SIGTERM)

    def test_stop3_sigkill_when_sigterm_is_ignored_too(self):
        sent, code = self.run_stubborn([signal.SIGINT, signal.SIGTERM])
        self.assertEqual(sent[:2], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(code, -SIGKILL)

    def test_default_constants(self):
        self.assertGreater(server.STOP_GRACE_SECONDS, 35 + 30 + 5 - 1)
        self.assertEqual(server.START_TIMEOUT_SECONDS, 60)


class Lines(DashTestCase):
    FOUR = [f"line{i}@x.test" for i in range(1, 5)]

    def test_retry1_selection(self):
        for args, expected in ((("--lines", "3,1,3"), [1, 3]), (("--lines=3",), [3])):
            with self.subTest(args):
                mock = self.make_mock(hydrate_ms=100)
                result = self.run_gow(SCRIPT, mock, users_text(self.FOUR), *args, "--stagger", "0")
                self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), [self.FOUR[i - 1] for i in expected],
                                 result.output[-1500:])

    def test_retry1_bad_values(self):
        mock = self.make_mock()
        cases = [("--lines", v) for v in ("0", "-1", "abc", "1,,2", "1,", ",1", "1;rm", "1 2", "12345678", "")]
        cases += [("--lines",), ("--lines=",)]
        results = self.run_all([
            lambda c=case: self.run_gow(SCRIPT, mock, users_text(self.FOUR), *c, harness={}) for case in cases])
        for case, result in zip(cases, results):
            with self.subTest(case):
                self.assertEqual(result.returncode, 1, result.output[-500:])
                self.assertRegex(result.stdout, r"(?m)^Error: ")
                self.assertEqual(result.spy["launches"], 0)
        self.assertEqual(mock.total_requests, 0)

    def test_retry2_with_positional_numbers(self):
        mock = self.make_mock()
        result = self.run_gow(SCRIPT, mock, users_text(self.FOUR), "1", "2", "--lines", "1", harness={})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error: --lines cannot be combined with a line range", result.stdout)
        self.assertEqual(result.spy["launches"], 0)

    def test_retry2_lines_with_no_valid_account(self):
        text = "good@x.test:pw-value\n\n# comment-text\nsecretname@x.test secret-password\n"
        mock = self.make_mock({"good@x.test": Account(password="pw-value")}, hydrate_ms=100)
        result = self.run_gow(SCRIPT, mock, text, "--lines", "1,2,3,4", "--stagger", "0")
        for line in (2, 3, 4):
            with self.subTest(line):
                self.assertRegex(result.stdout, rf"\[WARNING\] Line {line} has no valid account")
        for leaked in ("comment-text", "secretname", "secret-password", "pw-value"):
            self.assertNotIn(leaked, result.output)
        self.assertEqual(mock.ledger_pairs(), [("good@x.test", "only")])

    def test_retry2_all_invalid(self):
        mock = self.make_mock()
        result = self.run_gow(SCRIPT, mock, "\n# only\n", "--lines", "1,2", harness={})
        self.assertIn("No valid accounts", result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.spy["launches"], 0)

    def test_retry3_other_scripts_refuse_the_flag(self):
        mock = self.make_mock()
        for script, args in (("claim_all.py", ("--lines", "1")), ("claim_batch.py", ("1", "2", "--lines", "1"))):
            with self.subTest(script):
                result = self.run_gow(script, mock, users_text(self.FOUR), *args, harness={})
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stdout, r"(?m)^Error: unknown option --lines$")
                self.assertEqual(result.spy["launches"], 0)


class Retry(DashTestCase):
    ACCOUNTS = {
        "one@x.test": Account(),
        "two@x.test": Account(password="other"),
        "three@x.test": Account(characters=("c1", "c2"), select_failures=("c2",)),
        "four@x.test": Account(collect_status=500),
        "five@x.test": Account(),
    }

    def finished_run(self, harness):
        dash = self.dash(self.ACCOUNTS, harness=harness, child_harness=FAST)
        self.assertEqual(dash.run(5), 200)
        state = dash.wait_state(lambda s: s["status"] == "finished" and not s["control"]["owned"], timeout=90,
                                message="the first run")
        self.assertEqual(state["retry_lines"], [2, 3, 4])
        return dash

    def test_retry4_runs_exactly_the_retry_lines(self):
        dash = self.finished_run({})
        first = len(dash.mock.logins)
        self.assertEqual(dash.retry(), 200)
        dash.wait_state(lambda s: not s["control"]["owned"] and s["control"]["phase"] is None
                        and len(s["recent_runs"]) == 2, timeout=90, message="the retry")
        self.assertEqual(dash.spawns()[-1]["argv"][-2:], ["--lines", "2,3,4"])
        self.assertEqual(sorted(e for e, _ in dash.mock.logins[first:]),
                         ["four@x.test", "three@x.test", "two@x.test"])

    def test_retry7_control_with_all_lines(self):
        dash = self.finished_run({"all_retry_lines": True})
        self.assertEqual(dash.retry(), 200)
        wait_until(lambda: len(dash.spawns()) == 2, 10, "the retry spawn")
        self.assertEqual(dash.spawns()[-1]["argv"][-2:], ["--lines", "1,2,3,4,5"])

    def test_retry5_the_request_cannot_choose_lines(self):
        dash = self.dash(harness={})
        for body in ({"lines": [1]}, {"args": ["--force-concurrency"]}, {"concurrency": 3, "lines": "1"},
                     {"concurrency": 3, "args": []}):
            with self.subTest(body):
                self.assertEqual(dash.post("/api/retry", body)[0], 400)
        self.assertEqual(dash.spawns(), [])

    def test_retry5_nothing_to_retry(self):
        dash = self.dash(harness={})
        self.assertEqual(dash.retry(), 409)
        self.assertEqual(dash.spawns(), [])

    def test_retry6_after_a_crash_only_failed_accounts_retry(self):
        accounts = {"quick@x.test": Account(), "denied@x.test": Account(password="other"), EMAIL: SLOW}
        dash = self.dash(accounts, child_harness=FAST)
        self.assertEqual(dash.run(3), 200)
        dash.wait_state(lambda s: s["totals"] and s["totals"]["done"] == 2, message="two accounts to finish")
        os.kill(dash.child_pid(), signal.SIGKILL)
        state = dash.wait_state(lambda s: s["status"] == "crashed", timeout=10, message="the crash")
        self.assertEqual(state["retry_lines"], [2])
        rows = {row["line"]: row["result"] for row in state["accounts"]}
        self.assertEqual(rows[3], "unfinished")
        self.assertEqual(state["totals"]["unfinished"], 1)


class Liveness(DashTestCase):
    def test_ev6_terminal_run_with_no_server_appears_afterwards(self):
        mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100)
        result, path = self.recording("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(read_events(path)[0]["source"], "terminal")

        dash = self.dash()
        os.makedirs(os.path.dirname(dash.events_file), mode=0o700, exist_ok=True)
        with open(path, "rb") as source, open(dash.events_file, "wb") as target:
            target.write(source.read())
        state = dash.state()
        self.assertEqual([run["source"] for run in state["recent_runs"]], ["terminal"])
        self.assertEqual(state["recent_runs"][0]["jewels"], 75)

    def test_live3_a_restarted_server_shows_the_run_and_cannot_stop_it(self):
        dash = self.dash({EMAIL: Account(vault_delay_ms=10000)}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["live"], message="the run")
        dash.kill_server()
        dash.start()
        state = dash.state()
        self.assertTrue(state["live"])
        self.assertFalse(state["control"]["owned"])
        self.assertEqual(state["status"], "running")
        self.assertEqual(dash.stop(), 409)
        state = dash.wait_state(lambda s: s["status"] == "finished", message="the run to finish alone")
        self.assertEqual(state["totals"]["claimed"], 1)
        self.assertEqual(dash.child_exit(), "0")

    def assert_crashed(self, dash, pid):
        os.kill(pid, signal.SIGKILL)
        began = time.monotonic()
        state = dash.wait_state(lambda s: s["status"] == "crashed", timeout=10, message="the crash")
        self.assertLessEqual(time.monotonic() - began, 2.5)
        self.assertEqual({row["result"] for row in state["accounts"]}, {"unfinished"})
        self.assertFalse(state["live"])

    def test_live4_a_killed_dashboard_run(self):
        dash = self.dash({EMAIL: SLOW}, child_harness=FAST)
        self.assertEqual(dash.run(), 200)
        dash.wait_state(lambda s: s["slots"], message="the account to start")
        self.assert_crashed(dash, dash.child_pid())
        wait_until(lambda: not dash.state()["control"]["owned"], 3, "Start to free up")

    def test_live4_a_killed_terminal_run(self):
        dash = self.dash({EMAIL: SLOW})
        process = self.terminal_run(dash)
        dash.wait_state(lambda s: s["slots"], message="the account to start")
        self.assert_crashed(dash, process.pid)
        process.wait(5)

    def test_live5_control_without_the_lock_test(self):
        dash = self.dash({EMAIL: SLOW}, harness={"no_lock_test": True})
        process = self.terminal_run(dash)
        dash.wait_state(lambda s: s["slots"], message="the account to start")
        os.kill(process.pid, signal.SIGKILL)
        process.wait(5)
        time.sleep(4)
        state = dash.state()
        self.assertEqual(state["status"], "running")
        self.assertTrue(state["live"])

    def test_live6_hostile_run_ids_over_the_server(self):
        dash = self.dash()
        keep = os.path.join(dash.temp_dir, "keep")
        with open(keep, "w") as f:
            f.write("x")
        write_events(dash.events_file, [run_start("../../keep", 1000.0), run_start("20261101T000000Z-1/../y", 1000.0)])
        state = dash.state()
        self.assertEqual(state["recent_runs"], [])
        self.assertTrue(os.path.exists(keep))

    def test_deviation_a_crashed_run_drops_its_stale_lock(self):
        dash = self.dash()
        run = "20261101T000000Z-1"
        target = events.lock_path(dash.events_file, run)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with open(target, "w"):
            pass
        write_events(dash.events_file, [run_start(run, 1000.0)])
        self.assertEqual(dash.state()["status"], "crashed")
        self.assertFalse(os.path.exists(target))
        self.assertEqual(dash.spawns(), [], "a run id in a fixture must never become a pid to clean up")


class RealRate(DashTestCase):
    def test_spc4_a_real_stopped_run_uses_run_end_and_counts_the_interrupted_claim(self):
        # As in test_ev9: c2's switch never answers, which holds the run between characters
        account = Account(characters=("c1", "c2", "c3"), select_failures=("c2",), select_failure_status=0)
        dash = self.dash({EMAIL: account}, child_harness=FAST)
        self.assertEqual(dash.run(1), 200)
        wait_until(lambda: dash.mock.ledger_pairs(), 30, "the first claim")
        time.sleep(1)
        self.assertEqual(dash.stop(), 200)
        state = dash.wait_state(lambda s: not s["control"]["owned"], timeout=40, message="the run to end")
        recorded = dash.recorded()
        started = next(e for e in recorded if e["event"] == "run_start")["ts"]
        ended = next(e for e in recorded if e["event"] == "run_end")
        (end,) = [e for e in recorded if e["event"] == "account_end"]
        self.assertEqual(ended["status"], "interrupted")
        self.assertIs(end["interrupted"], True)
        self.assertEqual(state["run"]["claims"], 1)
        self.assertEqual(state["run"]["seconds_per_claim"], round(ended["ts"] - started, 1))
        time.sleep(1.2)
        self.assertEqual(dash.state()["run"]["seconds_per_claim"], state["run"]["seconds_per_claim"])

    def test_spc4_a_real_crashed_run_uses_its_last_event(self):
        accounts = {"quick@x.test": Account(), EMAIL: SLOW}
        dash = self.dash(accounts, child_harness=FAST)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["totals"] and s["totals"]["claimed"] == 1, timeout=30, message="the first claim")
        child = dash.child_pid()
        self.assertIn(SCRIPT, command_of(str(child)))
        os.kill(child, signal.SIGKILL)
        state = dash.wait_state(lambda s: s["status"] == "crashed", timeout=10, message="the crash")
        recorded = dash.recorded()
        self.assertEqual(state["run"]["seconds_per_claim"], round(recorded[-1]["ts"] - recorded[0]["ts"], 1))
        time.sleep(1.2)
        self.assertEqual(dash.state()["run"]["seconds_per_claim"], state["run"]["seconds_per_claim"])


class StartFailure(DashTestCase):
    def test_a_child_that_exits_before_run_start_is_reported(self):
        dash = self.dash()
        os.remove(dash.users_file)
        self.assertEqual(dash.run(), 200)
        state = dash.wait_state(lambda s: s["control"]["error"], timeout=20, message="the start failure")
        self.assertEqual(state["control"]["error"], "Run could not start (exit 1)")
        self.assertFalse(state["control"]["owned"])


if __name__ == "__main__":
    unittest.main()
