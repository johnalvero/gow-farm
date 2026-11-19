"""Dashboard D8: the carried-over select_character change, and the event recorder."""

import ast
import glob
import json
import os
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock as unittest_mock

from dash_support import DashTestCase, account_end, event, read_events, run_copy, run_start, write_events
from gow_support import PROJECT, Account, range_args, users_text
from test_s_units import FakePage, FakeRequest, FakeResponse, constants, quietly, CALLBACK_PATH, SELECT_PATH, TOKEN_PATH

import claim_common
from dashboard import events, store

SCRIPT = "claim_all_characters.py"
ALL = (SCRIPT, "claim_batch.py", "claim_all.py")
EMAIL = "events@x.test"
TWO = ("c1", "c2")
RUN = "20261101T000000Z-4242"


class Card:
    def __init__(self, page, status=None, signs_in=False):
        self.page = page
        self.status = status
        self.signs_in = signs_in

    async def get_attribute(self, name):
        return "css-1"

    async def click(self):
        if self.signs_in:
            self.page.fire("request", FakeRequest(TOKEN_PATH))
            self.page.fire("request", FakeRequest(CALLBACK_PATH))
            self.page.fire("response", FakeResponse(CALLBACK_PATH))
        if self.status:
            self.page.fire("response", FakeResponse(SELECT_PATH, self.status))


class CarriedOver(DashTestCase):
    def select(self, **card):
        page = FakePage(statuses=())
        with constants(SELECT_PROFILE_TIMEOUT_MS=50, SIGN_IN_START_TIMEOUT_SECONDS=0.01):
            result, error, out = quietly(claim_common.select_character(page, Card(page, **card), EMAIL))
        self.assertIsNone(error, out)
        self.assertEqual(page.listeners(), 0)
        return result, out

    def test_c1_1_no_response_with_sign_in_done_is_true_and_still_logged(self):
        result, out = self.select(signs_in=True)
        self.assertIs(result, True)
        self.assertIn("select-profile not observed", out)

    def test_c1_2_no_response_with_sign_in_not_done_is_false(self):
        self.assertIs(self.select()[0], False)

    def test_c1_2_non_ok_stays_false_even_when_sign_in_finished(self):
        self.assertIs(self.select(status=500, signs_in=True)[0], False)

    ACCOUNT = {EMAIL: Account(characters=TWO, select_never_answers=("c2",))}

    def test_c1_3_end_to_end(self):
        mock = self.make_mock(self.ACCOUNT, hydrate_ms=100)
        result = self.run_gow(SCRIPT, mock, users_text([EMAIL]), "1", "1", "--stagger", "0",
                              harness={"select_profile_timeout_ms": 1500})
        self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c2")], result.output[-2500:])
        self.assertIn("✅ 2/2 chars", result.stdout)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 150)
        self.assertEqual(result.returncode, 0)

    def test_c1_4_control_without_the_clause(self):
        mock = self.make_mock(self.ACCOUNT, hydrate_ms=100)
        result = run_copy(self, SCRIPT, mock, users_text([EMAIL]), ("1", "1", "--stagger", "0"),
                          harness={"select_profile_timeout_ms": 1500},
                          replace=[("claim_common.py", ' or (response is None and sign_in["done"].is_set())', "")])
        self.assertIn("✅ 1/2 chars", result.stdout, result.output[-2500:])
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])


def of_kind(recorded, name):
    return [entry for entry in recorded if entry["event"] == name]


class Recording(DashTestCase):
    def test_ev1_ev2_a_run_is_recorded_in_order_on_all_three_scripts(self):
        addresses = ["first@x.test", "second@x.test"]
        def run(script):
            mock = self.make_mock(hydrate_ms=100)
            return mock, self.recording(script, mock, users_text(addresses), *range_args(script, 2), "--stagger", "0.1")

        for script, (mock, (result, path)) in zip(ALL, self.run_all([lambda s=s: run(s) for s in ALL])):
            with self.subTest(script):
                recorded = read_events(path)
                self.assertEqual(recorded[0]["event"], "run_start")
                self.assertEqual(recorded[0]["script"], script)
                self.assertEqual(recorded[-1]["event"], "run_end")
                self.assertEqual(recorded[-1]["status"], "finished")
                self.assertEqual({entry["v"] for entry in recorded}, {1})
                self.assertEqual({entry["run"] for entry in recorded}, {recorded[0]["run"]})
                for line in (1, 2):
                    mine = [entry["event"] for entry in recorded if entry.get("line") == line]
                    self.assertEqual(mine[0], "account_start")
                    self.assertEqual(mine[-1], "account_end")
                    self.assertIn("stage", mine)
                    stages = [e["stage"] for e in of_kind(recorded, "stage") if e["line"] == line]
                    self.assertEqual(stages, ["login", "signin", "vault", "claim"])

    def test_ev3_mixed_run_agrees_with_the_summary_and_the_ledger(self):
        accounts = {
            "fresh@x.test": Account(),
            "cool@x.test": Account(vault="cooldown"),
            "denied@x.test": Account(password="other"),
            "fresh2@x.test": Account(),
        }
        def run(script):
            mock = self.make_mock(accounts, hydrate_ms=100)
            return mock, self.recording(script, mock, users_text(accounts), *range_args(script, 4),
                                        "--concurrency", "3", "--stagger", "0.1")

        for script, (mock, (result, path)) in zip(ALL, self.run_all([lambda s=s: run(s) for s in ALL])):
            with self.subTest(script):
                ends = sorted(of_kind(read_events(path), "account_end"), key=lambda e: e["line"])
                self.assertEqual([e["line"] for e in ends], [1, 2, 3, 4])
                rows = result.summary_rows()
                self.assertEqual([e["success"] for e in ends], [ok for ok, _ in rows])
                self.assertEqual([e["label"][0] for e in ends], [email[0] for _, email in rows])
                claimed = sum(e["claimed"] for e in ends)
                self.assertEqual(claimed * 75, result.summary_value("Total Jewels Claimed"))
                self.assertEqual(claimed, len(mock.ledger_pairs()))
                self.assertEqual(sum(e["already"] for e in ends), result.summary_value("Characters Already Claimed"))

    CASES = [
        ("load failure", Account(), {"login_page_status": 503}, {}, ("page_load_failed", 503)),
        ("401", Account(password="other"), {}, {}, ("login_rejected", 401)),
        ("no button", Account(vault="nothing"), {}, {}, ("no_claim_button", None)),
        ("no request", Account(claim_no_request=True), {}, {}, ("claim_no_request", None)),
        ("no response", Account(collect_ms=-1), {}, {"claim_response_timeout_ms": 2000}, ("claim_no_response", None)),
        ("error body", Account(collect_body='{"error":"no"}'), {}, {}, ("claim_rejected", 200)),
        ("unreadable", Account(collect_body="<html>"), {}, {}, ("claim_unreadable", 200)),
        ("deadline", Account(freeze_on="vault"), {}, {"account_deadline_seconds": 8}, ("deadline", None)),
        ("worker exception", Account(), {}, {"raise_worker_for": [EMAIL]}, ("error", None)),
    ]

    def assert_reason(self, path, expected, result):
        name, status = expected
        found = of_kind(read_events(path), "reason")
        self.assertEqual([(e["reason"], e.get("status")) for e in found], [(name, status)], result.output[-1200:])

    def test_ev4_each_failure_gives_its_enum_on_all_three_scripts(self):
        for name, account, mock_options, harness, expected in self.CASES:
            def run(script, a=account, m=mock_options, h=harness):
                mock = self.make_mock({EMAIL: a}, hydrate_ms=100, **m)
                return self.recording(script, mock, users_text([EMAIL]), *range_args(script, 1), "--stagger", "0",
                                      harness=h)

            for script, (result, path) in zip(ALL, self.run_all([lambda s=s: run(s) for s in ALL])):
                with self.subTest(name, script=script):
                    self.assert_reason(path, expected, result)
                    end = of_kind(read_events(path), "account_end")[0]
                    self.assertFalse(end["success"])
                    self.assertEqual(end["timed_out"], name == "deadline")

    def test_ev4_failed_switch(self):
        mock = self.make_mock({EMAIL: Account(characters=TWO, select_failures=("c2",))}, hydrate_ms=100)
        result, path = self.recording(SCRIPT, mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assert_reason(path, ("switch_failed", None), result)

    def test_ev5_file_modes_and_ignore_rules(self):
        mock = self.make_mock(hydrate_ms=100)
        _, path = self.recording("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        paths = ["dashboard/data/events.jsonl", "dashboard/data/last-run.log"]
        done = subprocess.run(["git", "-C", PROJECT, "check-ignore", *paths], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.split(), paths)

    def test_ev7_rotation_keeps_both_files_readable_and_at_most_two(self):
        temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, temp, True)
        path = os.path.join(temp, "data", "events.jsonl")
        mock = self.make_mock(hydrate_ms=100)
        for number in range(3):
            self.recording("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0",
                           harness={"max_event_bytes": 100}, events_file=path)
            time.sleep(1.1)     # run ids have one-second resolution

        names = sorted(n for n in os.listdir(os.path.dirname(path)) if not n.endswith(".locks"))
        self.assertEqual(names, ["events.jsonl", "events.jsonl.1"])
        state = store.EventStore(path).snapshot()
        self.assertEqual(len(state["recent_runs"]), 2)

    def test_ev8_no_event_file_without_a_chosen_path(self):
        """DATA1. In a copy of the project, so the real dashboard/data is never looked at"""
        for name, events_file in (("unset", None), ("empty", "")):
            with self.subTest(name):
                mock = self.make_mock(hydrate_ms=100)
                result = run_copy(self, "claim_batch.py", mock, users_text([EMAIL]), ("1", "1", "--stagger", "0"),
                                  events_file=events_file)
                self.assertEqual(result.returncode, 0, result.output[-800:])
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertFalse(os.path.exists(os.path.join(self.last_copy, "dashboard", "data")))

    def test_data1_control_each_rule_removed_in_the_copy_gives_a_data_folder(self):
        """The folder this makes is inside the temp copy: DEFAULT_PATH hangs off the copy's own __file__"""
        for name, events_file, old, new in (
                ("unset", None, 'if os.environ.get("GOW_BASE_URL"):', "if False:"),
                ("empty", "", "return os.path.abspath(configured) if configured else None",
                 "return os.path.abspath(configured) if configured else DEFAULT_PATH")):
            with self.subTest(name):
                mock = self.make_mock(hydrate_ms=100)
                run_copy(self, "claim_batch.py", mock, users_text([EMAIL]), ("1", "1", "--stagger", "0"),
                         replace=[("dashboard/events.py", old, new)], events_file=events_file)
                self.assertTrue(os.path.exists(os.path.join(self.last_copy, "dashboard", "data", "events.jsonl")))

    def test_events_path_rules(self):
        with unittest_mock.patch.dict(os.environ, {"GOW_BASE_URL": "http://127.0.0.1:9"}, clear=False):
            os.environ.pop("GOW_EVENTS_FILE", None)
            self.assertIsNone(events.events_path())
            os.environ["GOW_EVENTS_FILE"] = ""
            self.assertIsNone(events.events_path())
            os.environ["GOW_EVENTS_FILE"] = "relative.jsonl"
            self.assertEqual(events.events_path(), os.path.abspath("relative.jsonl"))
            del os.environ["GOW_EVENTS_FILE"]
            del os.environ["GOW_BASE_URL"]
            self.assertEqual(events.events_path(), events.DEFAULT_PATH)


class WritePath(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.path = os.path.join(self.temp, "data", "events.jsonl")
        for patch in (unittest_mock.patch.dict(os.environ, {"GOW_EVENTS_FILE": self.path}),
                      unittest_mock.patch("sys.argv", [os.path.join(PROJECT, "claim_batch.py")])):
            patch.start()
            self.addCleanup(patch.stop)

    def started(self):
        events.run_started(1, 3, 10, [1])
        self.assertIsNotNone(events.recorder)
        self.addCleanup(self.finish, events.recorder)
        return events.recorder

    def finish(self, recorder):
        """Join the writer here, or it can recreate the temp directory after it is removed"""
        recorder.stopped = True
        try:
            recorder.queue.put_nowait(None)
        except queue.Full:
            pass
        recorder.thread.join(5)
        events.recorder = None

    GOOD = {"v": 1, "run": RUN, "ts": 1.0, "event": "stage", "line": 1, "stage": "login"}

    def test_sec2_validate_refuses_each_shape(self):
        self.assertTrue(events.validate_event(self.GOOD))
        for name, changed in (("unknown field", {"note": "x"}), ("outside the enum", {"stage": "hacked"}),
                              ("free text", {"stage": "john@gmail.com"}), ("bool line", {"line": True}),
                              ("bad run id", {"run": "../../x"}), ("wrong version", {"v": 2})):
            with self.subTest(name):
                self.assertFalse(events.validate_event({**self.GOOD, **changed}))
        for label in ("jo***@g***", "j***@gmail.com", "j***@", "<img src=x>", "j*** "):
            with self.subTest(label):
                self.assertFalse(events.validate_event({"v": 1, "run": RUN, "ts": 1.0, "event": "account_start",
                                                        "line": 1, "label": label}))
        self.assertFalse(events.validate_event("not a dict"))
        self.assertFalse(events.validate_event({"event": "nope"}))

    def test_sec2_the_write_path_writes_nothing_for_a_refused_event(self):
        recorder = self.started()
        recorder.put("stage", line=1, stage="login")
        recorder.put("stage", line=1, stage="john@gmail.com")
        recorder.put("stage", line=1, stage="login", note="john@gmail.com")
        recorder.put("account_start", line=1, label="<img src=x>")
        recorder.put("made_up", line=1)
        events.run_finished("finished")
        events.close()

        with open(self.path) as f:
            text = f.read()
        self.assertNotIn("john", text)
        self.assertNotIn("<img", text)
        self.assertNotIn("made_up", text)
        recorded = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([e["event"] for e in recorded], ["run_start", "stage", "run_end"])
        self.assertEqual(recorded[-1]["dropped"], 4)

    def test_sec3_mask(self):
        for email, expected in (("john@gmail.com", "j***@g***"), ("a@x.test", "a***@x***"), ("john", "j***")):
            with self.subTest(email):
                self.assertEqual(events.mask_account(email), expected)

    def test_sec3_never_more_than_one_character_of_a_part(self):
        for email in ("john.smith+tag@mail.example.com", "a@b@c.test", "ünï@x.test", "@x.test", "x@", "",
                      "<img src=x>@y", "\"q\"@z.test", "日本語@例え.test"):
            with self.subTest(email):
                label = events.mask_account(email)
                self.assertTrue(events.is_label(label), label)
                self.assertRegex(label, r"^.\*\*\*(@.\*\*\*)?$")

    def test_iso5_a_full_queue_never_blocks_and_run_end_reports_the_drops(self):
        gate = threading.Event()
        real_write = events.write_line

        def held(fd, data):
            gate.wait(10)
            real_write(fd, data)

        with unittest_mock.patch.object(events, "write_line", held), \
                unittest_mock.patch.object(events, "MAX_QUEUE", 10):
            recorder = self.started()
            time.sleep(0.1)         # the writer takes run_start, and holds there
            began = time.monotonic()
            for _ in range(200):
                recorder.put("stage", line=1, stage="login")
            self.assertLess(time.monotonic() - began, 0.1)
            self.assertEqual(recorder.dropped, 190)

            gate.set()
            while not recorder.queue.empty():
                time.sleep(0.01)
            events.run_finished("finished")
            events.close()

        ended = of_kind(read_events(self.path), "run_end")
        self.assertEqual(ended[0]["dropped"], 190)

    def test_iso5_run_end_on_a_full_queue_is_dropped_without_blocking(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def held(fd, data):
            gate.wait(5)

        with unittest_mock.patch.object(events, "write_line", held), \
                unittest_mock.patch.object(events, "MAX_QUEUE", 2):
            recorder = self.started()
            time.sleep(0.1)
            for _ in range(5):
                recorder.put("stage", line=1, stage="login")
            began = time.monotonic()
            events.run_finished("finished")
            events.close()
            self.assertLess(time.monotonic() - began, events.CLOSE_TIMEOUT_SECONDS + 0.5)
            gate.set()
            while not recorder.queue.empty():
                time.sleep(0.01)

    def test_public_functions_are_silent_when_recording_is_off(self):
        events.recorder = None
        events.account_started(1, "a@x.test")
        events.stage("a@x.test", "login")
        events.reason("a@x.test", "error")
        events.account_finished(1, "a@x.test", "claimed")
        events.run_finished("finished")
        events.close()

    def test_an_account_never_started_is_ignored_by_stage_and_reason(self):
        recorder = self.started()
        events.stage("nobody@x.test", "login")
        events.reason("nobody@x.test", "error")
        self.assertEqual(recorder.dropped, 0)
        queued = []
        while not recorder.queue.empty():
            queued.append(recorder.queue.get_nowait())
        self.assertEqual([e for e in queued if e and e["event"] in ("stage", "reason")], [])

    def test_unknown_script_name_turns_recording_off(self):
        """The Coder's note: an unknown sys.argv[0] fails the enum, so nothing is written"""
        with unittest_mock.patch("sys.argv", ["other.py"]):
            events.run_started(1, 3, 10, [1])
            self.addCleanup(lambda: setattr(events, "recorder", None))
            events.run_finished("finished")
            events.close()
        self.assertFalse(os.path.exists(self.path))

    def test_deviation_lock_file_is_removed_once_the_run_has_ended(self):
        self.started()
        events.run_finished("finished")
        events.close()
        self.assertEqual(os.listdir(f"{self.path}.locks"), [])

    def test_result_shapes_normalize(self):
        self.assertEqual(events.result_counts({"total": 3, "claimed": 1, "already": 1}), (3, 1, 1))
        self.assertEqual(events.result_counts(claim_common.CLAIMED), (1, 1, 0))
        self.assertEqual(events.result_counts(claim_common.ALREADY_CLAIMED), (1, 0, 1))
        self.assertEqual(events.result_counts(claim_common.FAILED), (0, 0, 0))


class FullLabels(unittest.TestCase):
    def test_a_plain_email_is_kept_whole(self):
        for email in ("john@gmail.com", "a.b+c@mail.example.com", "x_y-z%1@sub.do-main.test"):
            with self.subTest(email):
                self.assertEqual(events.account_label(email), email)

    def test_em4_anything_else_falls_back_to_the_mask(self):
        for value, expected in (("hunter2", "h***"), ("<img/src=x>@y.test", "?***@y***"), ('"q"@x.test', "?***@x***"),
                                ("a@localhost", "a***@l***"), ("ünï@x.test", "?***@x***"),
                                ("a" * 293 + "@x.test", "a***@x***")):
            with self.subTest(value[:24]):
                self.assertEqual(events.account_label(value), expected)
                self.assertTrue(events.is_label(events.account_label(value)))

    def test_em5_is_label_accepts_either_shape(self):
        for label in ("john@gmail.com", "a.b+c@mail.example.com", "j***@g***", "j***"):
            with self.subTest(label):
                self.assertTrue(events.is_label(label))

    def test_em5_is_label_refuses_the_rest(self):
        for label in ("<img src=x>", "john @gmail.com", "john@gmail.com\n", "j***@", "a" * 40 + "@" + "b" * 220 + ".test",
                      "hunter2", "a@localhost", "", None, 5):
            with self.subTest(repr(label)[:24]):
                self.assertFalse(events.is_label(label))

    def test_em5_the_limits(self):
        longest = "a" * 64 + "@" + "b" * 184 + ".test"
        self.assertEqual(len(longest), 254)
        self.assertTrue(events.is_label(longest))
        self.assertFalse(events.is_label("a" * 64 + "@" + "b" * 185 + ".test"))
        self.assertFalse(events.is_label("a" * 65 + "@b.test"))

    def test_em8_the_schema_has_no_new_field(self):
        self.assertEqual({name: sorted(fields) for name, fields in events.EVENT_FIELDS.items()}, {
            "run_start": ["concurrency", "lines", "script", "source", "stagger", "total"],
            "account_start": ["label", "line"],
            "stage": ["line", "stage"],
            "reason": ["line", "reason", "status"],
            "account_end": ["already", "characters", "claimed", "interrupted", "label", "line", "seconds",
                            "success", "timed_out"],
            "run_end": ["dropped", "status"],
        })

    def test_pw3_no_function_takes_a_password(self):
        with open(os.path.join(PROJECT, "dashboard", "events.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertGreater(len(functions), 20)
        for function in functions:
            names = [a.arg for a in function.args.posonlyargs + function.args.args + function.args.kwonlyargs]
            with self.subTest(function.name):
                self.assertEqual([n for n in names if "pass" in n.lower() or "secret" in n.lower()], [])
        arguments = {f.name: [a.arg for a in f.args.args] for f in functions}
        self.assertEqual(arguments["account_started"], ["line", "email"])
        self.assertEqual(arguments["account_finished"], ["line", "email", "result", "timed_out", "interrupted"])

    def test_pw3_no_recorder_call_is_given_a_password(self):
        with open(os.path.join(PROJECT, "claim_common.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())

        def mentions(node, names):
            for part in ast.walk(node):
                text = part.id if isinstance(part, ast.Name) else part.attr if isinstance(part, ast.Attribute) else (
                    part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "")
                if "password" in text.lower() or text in names:
                    return True
            return False

        carriers = set()
        for _ in range(3):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                        and mentions(node.value, carriers)):
                    carriers.add(node.targets[0].id)

        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "events"]
        self.assertGreater(len(calls), 15)
        for call in calls:
            with self.subTest(line=call.lineno):
                arguments = [*call.args, *[keyword.value for keyword in call.keywords]]
                self.assertEqual([ast.unparse(a) for a in arguments if mentions(a, carriers)], [])


REAL_FOLDER = ["dashboard", "data"]
FOLDER_EXEMPT = ("test_a_dashboard_run_needs_a_temp_event_file", "test_ev5_file_modes_and_ignore_rules")


def dotted(node):
    """"self.base" for an attribute chain, "base" for a name, otherwise None"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = dotted(node.value)
        return inner and f"{inner}.{node.attr}"
    return None


def tidy(segments):
    out = []
    for segment in segments:
        if segment == ".." and out:
            out.pop()
        elif segment not in ("", ".", ".."):
            out.append(segment)
    return out


def path_of(node, known):
    """The path segments a test expression builds. "?" stands for anything unknown"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split("/")
    if isinstance(node, ast.Call):
        name = dotted(node.func) or ""
        if name.split(".")[-1] == "dirname" and node.args:
            return [*path_of(node.args[0], known), ".."]
        if name.split(".")[-1] in ("join", "Path", "PurePath"):
            return [segment for arg in node.args for segment in path_of(arg, known)]
        if name.split(".")[-1] == "joinpath" or (isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath"):
            return [*path_of(node.func.value, known), *[s for arg in node.args for s in path_of(arg, known)]]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        return [*path_of(node.left, known), *path_of(node.right, known)]
    if isinstance(node, ast.JoinedStr):
        return [segment for value in node.values for segment in path_of(value, known)]
    if isinstance(node, ast.FormattedValue):
        return path_of(node.value, known)
    name = dotted(node)
    return known.get(name, ["?"]) if name else ["?"]


def real_folder_uses(source):
    """(function, line) for each expression that builds a path into the real data folder,
    following a value built on an earlier line. A string given to an assertion is only text"""
    tree = ast.parse(source)
    known = {"PROJECT": [], "TESTS_DIR": ["tests"]}
    for _ in range(4):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and dotted(node.targets[0]):
                built = tidy(path_of(node.value, known))
                is_root = built == [] and dotted(node.value) in known
                if is_root or (built[:1] == ["dashboard"] and "?" not in built):
                    known[dotted(node.targets[0])] = built

    uses = []

    def visit(node, function, quoted):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if not (quoted and isinstance(node, ast.Constant)) and isinstance(
                node, (ast.Constant, ast.Call, ast.BinOp, ast.JoinedStr)):
            leading_slash = isinstance(node, ast.Constant) and str(node.value).startswith("/")
            if not leading_slash and tidy(path_of(node, known))[:2] == REAL_FOLDER:
                uses.append((function, node.lineno))
        checks = isinstance(node, ast.Call) and (dotted(node.func) or "").endswith(("assertIn", "assertNotIn"))
        for child in ast.iter_child_nodes(node):
            visit(child, function, checks)

    visit(tree, None, False)
    return uses


class Tree(unittest.TestCase):
    def sources(self):
        paths = glob.glob(os.path.join(PROJECT, "tests", "*.py"))
        self.assertGreater(len(paths), 20)
        for path in paths:
            with open(path, encoding="utf-8") as f:
                yield os.path.basename(path), f.read()

    def test_data2_tests_never_name_the_real_data_or_its_port(self):
        banned = ("REAL" + "_DATA", "87" + "65")
        exempt_found = set()
        for name, text in self.sources():
            with self.subTest(name):
                for word in banned:
                    self.assertNotIn(word, text)
                uses = real_folder_uses(text)
                exempt_found |= {function for function, _ in uses if function in FOLDER_EXEMPT}
                self.assertEqual([use for use in uses if use[0] not in FOLDER_EXEMPT], [])
        self.assertEqual(sorted(exempt_found), sorted(FOLDER_EXEMPT))

    def test_data2_the_copy_helper_leaves_any_data_folder_out(self):
        with open(os.path.join(PROJECT, "tests", "dash_support.py"), encoding="utf-8") as f:
            self.assertIn('ignore_patterns("__pycache__", "data")', f.read())


class SwappedLine(DashTestCase):
    """EM3: a password in the email column, from a line with its two parts swapped"""

    SWAPPED = "hunter2"

    def run_swapped(self, *controls):
        accounts = {self.SWAPPED: Account(password="user@x.test")}
        child_harness = {"default_stagger": 0.1, **{name: True for name in controls}}
        dash = self.dash(accounts, users="hunter2:user@x.test\n", child_harness=child_harness)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["run"] and not s["live"] and not s["control"]["owned"], timeout=40, message="the run")
        self.assertEqual(dash.mock.ledger_pairs(), [(self.SWAPPED, "only")])
        return dash

    def detector(self, dash):
        """Every event and lock file, and every response"""
        found = []
        folder = os.path.dirname(dash.events_file)
        for root, directories, names in os.walk(folder):
            for name in names + directories:
                if self.SWAPPED in name:
                    found.append(f"name {name}")
            for name in names:
                if name != "last-run.log":
                    with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                        if self.SWAPPED in f.read():
                            found.append(name)
        for path in ("/", "/app.js", "/app.css", "/api/state"):
            if self.SWAPPED in dash.get(path)[2].decode():
                found.append(path)
        return found

    def test_em3_the_value_is_masked_everywhere(self):
        dash = self.run_swapped()
        self.assertEqual({e["label"] for e in dash.recorded() if "label" in e}, {"h***"})
        self.assertEqual(self.detector(dash), [])
        self.assertEqual([row["label"] for row in dash.state()["accounts"]], ["h***"])

    def test_em3_the_writer_allowlist_alone_stops_the_value(self):
        dash = self.run_swapped("account_label_identity")
        # No h*** either: the switch took effect, and the writer dropped what it made
        self.assertEqual({e["label"] for e in dash.recorded() if "label" in e}, set())
        self.assertEqual(self.detector(dash), [])

    def test_em3_with_the_writer_allowlist_off_too(self):
        dash = self.run_swapped("account_label_identity", "events_no_validate")
        self.assertIn("events.jsonl", self.detector(dash))


class SecondsPerClaim(unittest.TestCase):
    T = 1000.0

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.path = os.path.join(self.temp, "data", "events.jsonl")

    def ended(self, claims, already=0, failed=0, seconds=176):
        ts = self.T + 1
        return [
            run_start(RUN, self.T, total=claims + already + failed),
            *[account_end(RUN, ts, line) for line in range(1, claims + 1)],
            *[account_end(RUN, ts, claims + line, claimed=0, already=1) for line in range(1, already + 1)],
            *[account_end(RUN, ts, claims + already + line, claimed=0, success=False) for line in range(1, failed + 1)],
            event("run_end", RUN, self.T + seconds, status="finished", dropped=0),
        ]

    def state(self, entries):
        if os.path.exists(self.path):
            os.remove(self.path)
        write_events(self.path, entries)
        return store.EventStore(self.path).snapshot()

    def test_spc1_ten_claims_in_176_seconds(self):
        run = self.state(self.ended(10))["run"]
        self.assertEqual(run["seconds_per_claim"], 17.6)
        self.assertEqual(run["claims"], 10)

    def test_spc1_already_and_failed_are_not_counted(self):
        run = self.state(self.ended(10, already=5, failed=5))["run"]
        self.assertEqual(run["seconds_per_claim"], 17.6)
        self.assertEqual(run["claims"], 10)

    def test_spc9_control_with_already_added_spc1_fails(self):
        def with_already(reader, run):
            return sum(account["claimed"] + account["already"] for account in run["accounts"].values())

        with unittest_mock.patch.object(store.EventStore, "claim_count", with_already):
            run = self.state(self.ended(10, already=5, failed=5))["run"]
        self.assertEqual(run["claims"], 15)
        self.assertEqual(run["seconds_per_claim"], 11.7)
        with self.assertRaises(AssertionError):
            self.assertEqual(run["seconds_per_claim"], 17.6)

    def test_spc2_no_claims_is_null(self):
        for name, entries in (("failed only", self.ended(0, failed=2)), ("already only", self.ended(0, already=3))):
            with self.subTest(name):
                state = self.state(entries)
                self.assertIsNone(state["run"]["seconds_per_claim"])
                self.assertEqual(state["run"]["claims"], 0)
                self.assertIn('"seconds_per_claim": null', json.dumps(state))
                self.assertIsNone(state["recent_runs"][0]["seconds_per_claim"])

    def live(self, claims):
        write_events(self.path, [run_start(RUN, self.T, total=9),
                                 *[account_end(RUN, self.T + 1, line) for line in range(1, claims + 1)]])
        reader = store.EventStore(self.path)
        reader.refresh()
        return reader, reader.runs[RUN]

    def test_spc3_a_live_run_uses_now(self):
        reader, run = self.live(4)
        self.assertEqual(reader.seconds_per_claim(run, self.T + 60), 15.0)
        self.assertEqual(reader.seconds_per_claim(run, self.T + 120), 30.0)

    def test_spc4_a_stopped_run_uses_run_end_and_counts_interrupted(self):
        run = self.state([
            run_start(RUN, self.T, total=3), account_end(RUN, self.T + 1, 1),
            account_end(RUN, self.T + 2, 2, claimed=2, characters=3, success=False, interrupted=True),
            event("run_end", RUN, self.T + 90, status="interrupted", dropped=0),
        ])["run"]
        self.assertEqual(run["claims"], 3)
        self.assertEqual(run["seconds_per_claim"], 30.0)

    def test_spc4_a_crashed_run_uses_its_last_event(self):
        write_events(self.path, [run_start(RUN, self.T, total=3), account_end(RUN, self.T + 40, 1),
                                 account_end(RUN, self.T + 80, 2)])
        reader = store.EventStore(self.path)
        state = reader.snapshot()
        self.assertEqual(state["status"], "crashed")
        self.assertEqual(state["run"]["seconds_per_claim"], 40.0)
        self.assertEqual(reader.snapshot()["run"]["seconds_per_claim"], 40.0)

    def test_spc6_every_recent_row_has_the_fields(self):
        row = self.state(self.ended(10))["recent_runs"][0]
        self.assertEqual((row["claims"], row["seconds_per_claim"]), (10, 17.6))

    def test_deviation_2_only_a_live_rows_duration_counts_up(self):
        reader, run = self.live(2)
        self.assertEqual(reader.recent_run(run, self.T + 50)["seconds"], 50)
        self.assertEqual(reader.recent_run(run, self.T + 80)["seconds"], 80)
        run["crashed"] = True
        self.assertEqual(reader.recent_run(run, self.T + 80)["seconds"], 1)
        run["crashed"] = False
        run["ended"], run["status"] = self.T + 30, "finished"
        self.assertEqual(reader.recent_run(run, self.T + 80)["seconds"], 30)

    def test_spc8_no_time_gives_none(self):
        for name, seconds in (("zero", 0), ("negative", -5)):
            with self.subTest(name):
                self.assertIsNone(self.state(self.ended(3, seconds=seconds))["run"]["seconds_per_claim"])

    def test_spc8_a_tiny_result_is_never_zero(self):
        entries = [run_start(RUN, self.T, total=1), account_end(RUN, self.T + 0.01, 1, claimed=1000, characters=1000),
                   event("run_end", RUN, self.T + 0.02, status="finished", dropped=0)]
        self.assertEqual(self.state(entries)["run"]["seconds_per_claim"], 0.1)


class Reader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.path = os.path.join(self.temp, "data", "events.jsonl")

    def snapshot(self, entries):
        write_events(self.path, entries)
        return store.EventStore(self.path).snapshot()

    def test_sec7_forged_lines_are_dropped(self):
        state = self.snapshot([
            run_start(RUN, 1000.0),
            event("account_start", RUN, 1001.0, line=1, label="<img src=victim>"),
            event("stage", RUN, 1002.0, line=1, stage="<img src=victim>"),
            event("reason", RUN, 1003.0, line=1, reason="<img src=victim>"),
            event("run_end", RUN, 1004.0, status="<img src=victim>", dropped=0),
            event("run_end", RUN, 1005.0, status="finished", dropped=0),
        ])
        self.assertNotIn("victim", json.dumps(state))
        self.assertEqual(state["status"], "finished")

    def test_live6_hostile_run_ids_are_ignored_and_touch_nothing(self):
        outside = os.path.join(self.temp, "keep.lock")
        with open(outside, "w") as f:
            f.write("x")
        hostile = ("../../x", "20261101T000000Z-1/../y", "../keep", "20261101T000000Z-1\n",
                   "20261101T000000Z-1/../../keep")
        state = self.snapshot([run_start(run, 1000.0) for run in hostile])
        self.assertEqual(state["recent_runs"], [])
        self.assertTrue(os.path.exists(outside))
        self.assertFalse(os.path.exists(f"{self.path}.locks"))

    def test_live7_bad_lines_are_skipped_around_good_ones(self):
        state = self.snapshot([
            b"not json at all\n",
            run_start(RUN, 1000.0, total=1),
            '{"v": 1, "truncated',
            {**run_start("20261101T000001Z-1", 1001.0), "v": 2},
            b"x" * (70 * 1024) + b"\n",
            b"\xff\xfe\n",
            "[]",
            '{"v":1,"run":"' + RUN + '","ts":NaN,"event":"run_end","status":"finished","dropped":0}',
            account_end(RUN, 1002.0, 1),
            event("run_end", RUN, 1003.0, status="finished", dropped=0),
            b'{"v":1,"run":"20261101T000002Z-2","ts":1',
        ])
        self.assertEqual(len(state["recent_runs"]), 1)
        self.assertEqual(state["totals"]["jewels"], 75)
        self.assertEqual(state["status"], "finished")

    def test_partial_last_line_is_read_once_it_completes(self):
        reader = store.EventStore(self.path)
        whole = json.dumps(run_start(RUN, 1000.0)).encode()
        write_events(self.path, [whole[:20]])
        self.assertEqual(reader.snapshot()["recent_runs"], [])
        write_events(self.path, [whole[20:] + b"\n", event("run_end", RUN, 1001.0, status="finished", dropped=0)])
        self.assertEqual(len(reader.snapshot()["recent_runs"]), 1)

    def test_deviation_interrupted_account_with_no_reason_reads_as_stopped(self):
        state = self.snapshot([
            run_start(RUN, 1000.0, total=1),
            account_end(RUN, 1002.0, 1, claimed=0, success=False, interrupted=True),
            event("run_end", RUN, 1003.0, status="interrupted", dropped=0),
        ])
        self.assertEqual(state["reasons"][0]["reason"], "stopped")
        self.assertEqual(state["status"], "stopped")

    def test_retry_lines_rule(self):
        state = self.snapshot([
            run_start(RUN, 1000.0, total=5),
            account_end(RUN, 1001.0, 1),
            account_end(RUN, 1002.0, 2, claimed=0, success=False),
            account_end(RUN, 1003.0, 3, claimed=1, characters=2),
            event("account_start", RUN, 1004.0, line=4, label="a***@x***"),
            account_end(RUN, 1005.0, 5, claimed=1, already=1, characters=2),
            event("run_end", RUN, 1006.0, status="finished", dropped=0),
        ])
        self.assertEqual(state["retry_lines"], [2, 3])
        rows = {row["line"]: row["result"] for row in state["accounts"]}
        self.assertEqual(rows[4], "unfinished")


if __name__ == "__main__":
    unittest.main()
