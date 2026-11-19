"""Source-level acceptance criteria, and the seams the safety of this suite relies on."""

import ast
import importlib
import os
import re
import subprocess
import unittest

from gow_support import PROJECT

import claim_common

ALL_FILES = ("claim_all_characters.py", "claim_all.py", "claim_batch.py", "claim_common.py")
SCRIPTS = ALL_FILES[:3]
REAL_HOST = "gameofwar-fireage.com"


def source(name):
    with open(os.path.join(PROJECT, name), encoding="utf-8") as f:
        return f.read()


def parents(tree):
    links = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            links[child] = node
    return links


def attribute_calls(tree, name):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name]


class WaitsInSource(unittest.TestCase):
    def test_no_networkidle_or_wait_for_load_state(self):
        for name in ALL_FILES:
            with self.subTest(name):
                text = source(name)
                self.assertNotIn("networkidle", text)
                self.assertNotIn("wait_for_load_state", text)

    def test_wait_for_timeout_only_in_url_branches(self):
        for name in ("claim_all_characters.py", "claim_common.py"):
            with self.subTest(name):
                self.assertEqual(attribute_calls(ast.parse(source(name)), "wait_for_timeout"), [])

        for name in ("claim_all.py", "claim_batch.py"):
            with self.subTest(name):
                tree = ast.parse(source(name))
                links = parents(tree)
                calls = attribute_calls(tree, "wait_for_timeout")
                self.assertEqual(len(calls), 2)
                for call in calls:
                    node, in_url_branch = call, False
                    while node in links:
                        node = links[node]
                        if isinstance(node, ast.If) and "current_url" in ast.unparse(node.test):
                            in_url_branch = True
                    self.assertTrue(in_url_branch, f"line {call.lineno} is outside the URL branch")

    def test_vault_helpers_never_read_raw_html(self):
        tree = ast.parse(source("claim_common.py"))
        self.assertEqual(attribute_calls(tree, "content"), [])


class SafetySeams(unittest.TestCase):
    """If these moved, the mock runs could reach the real site or real accounts"""

    def test_real_url_is_only_the_default_in_claim_common(self):
        for name in SCRIPTS:
            with self.subTest(name):
                self.assertNotIn(REAL_HOST, source(name))
        text = source("claim_common.py")
        self.assertEqual(text.count(REAL_HOST), 1)
        self.assertIn('os.environ.get("GOW_BASE_URL",', text)

    def test_scripts_read_users_file_from_the_override(self):
        for name in SCRIPTS:
            with self.subTest(name):
                self.assertIn('USERS_FILE = os.environ.get("GOW_USERS_FILE",', source(name))


class TRoundSource(unittest.TestCase):
    def test_t2_14_no_bare_except_remains(self):
        for name in ALL_FILES:
            with self.subTest(name):
                bare = [n.lineno for n in ast.walk(ast.parse(source(name)))
                        if isinstance(n, ast.ExceptHandler) and n.type is None]
                self.assertEqual(bare, [])

    def test_t2_17_readme_explains_the_deadline(self):
        text = source("README.md")
        section = text[text.index('"Account deadline of 900s passed"'):]
        section = section[:section.index("\n**", 1)]
        self.assertIn("15 minutes", section)
        self.assertIn("FAILED", section)
        self.assertRegex(section, r"(?i)characters it finished .*still count")

    def test_t3_1_document_path_is_set_once_inside_the_attempt_loop(self):
        tree = ast.parse(source("claim_common.py"))
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "vault_state")
        links = parents(function)
        assigned = [n for n in ast.walk(function) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                    and n.value.value is None and getattr(n.targets[0], "id", "") == "document_path"]
        self.assertEqual(len(assigned), 1)
        node = assigned[0]
        while node in links and not isinstance(node, ast.For):
            node = links[node]
        self.assertIsInstance(node, ast.For)


class ExceptionText(unittest.TestCase):
    """Playwright's later message lines can quote fill values and lose the email prefix"""

    def test_no_full_traceback_helpers(self):
        for name in ALL_FILES:
            for helper in ("print_exc", "format_exc", "print_exception", "format_exception"):
                with self.subTest(name, helper=helper):
                    self.assertEqual(attribute_calls(ast.parse(source(name)), helper), [])

    def test_exceptions_only_reach_output_through_first_line(self):
        for name in ALL_FILES:
            with self.subTest(name):
                tree = ast.parse(source(name))
                caught = {n.name for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name}
                for node in ast.walk(tree):
                    if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name):
                        self.assertNotIn(node.value.id, caught, f"line {node.lineno} formats a raw exception")


class MalformedLinesInSource(unittest.TestCase):
    def test_invalid_format_logs_never_interpolate_the_line(self):
        for name in SCRIPTS:
            with self.subTest(name):
                self.assertIsNone(re.search(r"Invalid format.*\{line\}", source(name)))


class OutcomesInSource(unittest.TestCase):
    """FAILED is a truthy string, so a leftover bool or `if success` would invert a failure"""

    def test_claim_functions_return_no_booleans(self):
        for name in SCRIPTS:
            tree = ast.parse(source(name))
            functions = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                         and n.name in ("claim_for_account", "claim_for_character")]
            self.assertTrue(functions)
            for function in functions:
                with self.subTest(name, function=function.name):
                    booleans = [n.lineno for n in ast.walk(function) if isinstance(n, ast.Return)
                                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, bool)]
                    self.assertEqual(booleans, [])

    def test_failed_account_gives_a_complete_failed_result(self):
        """Plan T2: still a complete failure with nothing recorded, now keeping what finished"""
        zeros = {"total": 0, "claimed": 0, "already": 0}
        for name in SCRIPTS:
            module = importlib.import_module(name[:-3])
            with self.subTest(name):
                self.assertIn("return", source(name)[source(name).index("def failed_account"):])
                if name == "claim_all_characters.py":
                    self.assertEqual(module.failed_account({}), zeros)
                    done = {"total": 3, "claimed": 1, "already": 1}
                    result = module.failed_account({"progress": done})
                    self.assertEqual(result, done)
                    self.assertIsNot(result, done)
                else:
                    self.assertEqual(module.failed_account({}), claim_common.FAILED)
                    self.assertEqual(module.failed_account({"outcome": claim_common.CLAIMED}), claim_common.CLAIMED)
                    self.assertEqual(module.failed_account({"outcome": claim_common.ALREADY_CLAIMED}),
                                     claim_common.ALREADY_CLAIMED)


class ClaimPathInSource(unittest.TestCase):
    URL_BRANCHES = {
        "claim_batch.py": """            if 'catalog' not in current_url and 'login' not in current_url:
                await page.wait_for_timeout(2000)
                try:
                    buttons = await page.locator('button[role="button"]').all()
                    if len(buttons) > 0:
                        await buttons[0].click()
                        await page.wait_for_timeout(3000)
                except Exception:
                    pass
""",
        "claim_all.py": """            if 'catalog' not in current_url and 'login' not in current_url:
                log(f"[{email}] Selecting character...")
                await page.wait_for_timeout(2000)

                try:
                    buttons = await page.locator('button[role="button"]').all()
                    if len(buttons) > 0:
                        await buttons[0].click()
                        log(f"[{email}] Selected character")
                        await page.wait_for_timeout(3000)
                except Exception:
                    log(f"[{email}] Character selection skipped", "WARNING")
""",
    }

    def test_nothing_says_unclear_and_claim_confirmed_is_gone(self):
        for name in ALL_FILES:
            with self.subTest(name):
                self.assertNotIn("unclear", source(name))
                self.assertNotIn("claim_confirmed", source(name))

    def test_scripts_never_click_claim_themselves(self):
        for name in SCRIPTS:
            with self.subTest(name):
                self.assertNotIn("claim_button(page).click", source(name))
                self.assertNotRegex(source(name), r"[\"']button:has-text\(.Claim")

    def test_one_claim_click_path_in_claim_common(self):
        tree = ast.parse(source("claim_common.py"))
        users = {}
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name) and n.func.id == "claim_button"]
                if calls:
                    users[function.name] = len(calls)
        self.assertEqual(users, {"vault_state": 1, "claim_accepted": 1})
        self.assertEqual(attribute_calls(tree, "click")[0].func.value.id, "target")
        self.assertNotIn("button.click", source("claim_common.py"))

    def test_url_branches_are_unchanged(self):
        for name, branch in self.URL_BRANCHES.items():
            with self.subTest(name):
                self.assertIn(branch, source(name))

    def test_console_capture_reads_only_the_message_text(self):
        text = source("claim_common.py")
        for banned in ("message.location", "message.args", "msg.location", "msg.args"):
            with self.subTest(banned):
                self.assertNotIn(banned, text)


class GitIgnore(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", "-C", PROJECT, *args], capture_output=True, text=True)

    def test_sensitive_and_generated_paths_are_ignored(self):
        paths = ["users.txt", "users-backup.txt", "__pycache__/x.pyc", "tests/__pycache__/x.pyc"]
        done = self.git("check-ignore", *paths)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), paths)

    def test_no_users_or_bytecode_path_is_tracked(self):
        tracked = self.git("ls-files").stdout.splitlines()
        self.assertTrue(tracked)
        self.assertEqual([p for p in tracked if "users" in os.path.basename(p) or "__pycache__" in p], [])


    def test_failure_screenshots_are_ignored(self):
        done = self.git("check-ignore", "screenshots/a_claim_failed.png")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["screenshots/a_claim_failed.png"])

    def test_reviewer_paths_are_ignored(self):
        paths = ["game-of-war-jewel-claim-api.md", "vault_page.html", "users_test.txt", "screenshots/x.png"]
        done = self.git("check-ignore", *paths)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), paths)

    def test_reviewer_paths_stay_out_of_git_status(self):
        status = self.git("status", "--short").stdout
        for name in ("game-of-war-jewel-claim-api.md", "vault_page.html", "users_test.txt", "screenshots/"):
            with self.subTest(name):
                self.assertNotIn(name, status)

    def test_earlier_entries_are_still_present(self):
        entries = source(".gitignore").split()
        for name in ("users.txt", "users-backup.txt", "__pycache__/"):
            with self.subTest(name):
                self.assertIn(name, entries)


class Readme(unittest.TestCase):
    def debug_section(self):
        text = source("README.md")
        start = text.index("### Debug Mode")
        return text[start:text.index("\n### ", start + 1)]

    def test_debug_mode_names_the_shared_browser_code(self):
        section = self.debug_section()
        self.assertIn("claim_common.py", section)
        self.assertIn("BrowserHolder.get()", section)
        self.assertNotRegex(section, r"claim_all_characters\.py.*line 110")
        self.assertNotIn("line 110", source("README.md"))

    def test_verification_is_described_as_the_api_response(self):
        text = source("README.md")
        self.assertNotIn("Checks page content", text)
        self.assertNotIn("unclear", text)
        verification = next(line for line in text.splitlines() if line.startswith("- **Verification**"))
        self.assertIn("/api/appcharge/collect-offer", verification)
        faq = next(line for line in text.splitlines() if line.startswith("**A:** Look for"))
        self.assertIn("/api/appcharge/collect-offer", faq)

    def test_command_reference_lists_the_new_flags(self):
        text = source("README.md")
        self.assertIn("| `--force-concurrency` |", text)
        self.assertIn("| `--no-block` |", text)

    def test_failure_screenshot_location_and_debug_note(self):
        text = source("README.md")
        self.assertIn("_claim_failed.png", text)
        self.assertIn("--no-block", self.debug_section())

    def test_users_file_override_is_documented_without_script_edits(self):
        text = source("README.md")
        self.assertNotIn("Modify script", text)
        self.assertRegex(text, r"set `GOW_USERS_FILE`\. All three scripts read it")

    def test_total_is_documented_as_new_claims_only(self):
        text = source("README.md")
        self.assertRegex(text, r"Total Jewels Claimed\*\*: New claims from this run only")

    def test_s5_always_on_dashboard_section(self):
        text = source("README.md")
        start = text.index("### Always-on dashboard")
        end = text.find("\n### ", start + 1)
        section = text[start:end if end != -1 else len(text)]
        for needle in ("bash dashboard/service.sh install", "bash dashboard/service.sh status",
                       "bash dashboard/service.sh uninstall", ".venv", "users.txt", "AirDrop", "Energy Saver"):
            with self.subTest(needle):
                self.assertIn(needle, section)

    def test_s5_always_on_dashboard_sits_in_the_dashboard_chapter(self):
        text = source("README.md")
        chapter = text.index("## 📊 Dashboard")
        section = text.index("### Always-on dashboard")
        self.assertLess(chapter, section)
        self.assertEqual(text.index("### Connection status") > section, True)
        self.assertNotIn("\n## ", text[chapter + 1:section])

    WAITS_FOR_A_RUN = r"(?i)\bwait(s|ing)?\b[^.\n]*claim run is going"

    def test_c35_always_on_says_run_it_again_not_waits(self):
        text = source("README.md")
        start = text.index("### Always-on dashboard")
        end = text.find("\n### ", start + 1)
        section = text[start:end if end != -1 else len(text)]
        self.assertIn("claim run is going", section)
        self.assertIn("run it again", section)
        self.assertEqual([line for line in text.splitlines() if re.search(self.WAITS_FOR_A_RUN, line)], [])

    def test_k11_the_c35_pattern_catches_the_old_wording(self):
        self.assertRegex("Uninstall waits while a claim run is going.", self.WAITS_FOR_A_RUN)

    def test_sample_output_totals_75_for_one_claim_plus_one_cooldown(self):
        text = source("README.md")
        self.assertIn("Characters Already Claimed: 1", text)
        self.assertIn("Total Jewels Claimed: 75 💎", text)
        sample = text[:text.index("Total Jewels Claimed: 75 💎")]
        self.assertIn("✓ Already claimed", sample[-2500:])
        self.assertIn("Claimed 75 jewels", sample)


class UpdateSource(unittest.TestCase):
    """in-app-update F1-F4"""
    FILES = ("dashboard/server.py", "dashboard/updater.py")
    FORBIDDEN_WORDS = ("removeprefix", "removesuffix", "functools.cache", "zoneinfo")
    FORBIDDEN_GIT = ("pip", "install", "push", "checkout", "stash", "clean")

    def test_f1_parses_as_python_3_8_without_newer_library_or_syntax(self):
        for name in self.FILES:
            with self.subTest(name):
                text = source(name)
                tree = ast.parse(text, feature_version=(3, 8))
                for word in self.FORBIDDEN_WORDS:
                    self.assertNotIn(word, text)
                self.assertNotRegex(text, r"(?m)^\s*match\s")
                self.assertFalse([n for n in ast.walk(tree) if type(n).__name__ == "Match"])
                self.assertNotRegex(text, r"\|\s*None\b|\bNone\s*\|")
                annotations = []
                for n in ast.walk(tree):
                    if isinstance(n, ast.arg) and n.annotation:
                        annotations.append(n.annotation)
                    elif isinstance(n, ast.AnnAssign):
                        annotations.append(n.annotation)
                    elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns:
                        annotations.append(n.returns)
                for annotation in annotations:
                    self.assertFalse([b for b in ast.walk(annotation)
                                      if isinstance(b, ast.BinOp) and isinstance(b.op, ast.BitOr)])

    def test_f1_the_checker_catches_what_it_checks_for(self):
        self.assertRaises(SyntaxError, ast.parse, "match x:\n    case 1: pass\n", feature_version=(3, 8))
        self.assertRegex("def f(x: str | None): pass", r"\|\s*None\b")

    def test_f2_updater_never_does_what_it_must_not(self):
        text = source("dashboard/updater.py")
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.keyword) and n.arg == "shell":
                self.assertIsInstance(n.value, ast.Constant)
                self.assertIs(n.value.value, False)
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                self.assertNotIn(n.value, self.FORBIDDEN_GIT)
        lists = [[e.value for e in n.elts if isinstance(e, ast.Constant)]
                 for n in ast.walk(tree) if isinstance(n, ast.List)]
        resets = [items for items in lists if "reset" in items]
        self.assertTrue(resets)
        for items in resets:
            self.assertEqual(items[:2], ["reset", "--keep"])
        merges = [items for items in lists if "merge" in items]
        self.assertTrue(merges)
        for items in merges:
            self.assertIn("--ff-only", items)
            self.assertIn("--no-overwrite-ignore", items)

    def test_f3_update_command_is_untouched(self):
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(PROJECT, "update.command")]).returncode, 0)
        shown = subprocess.run(["git", "-C", PROJECT, "show", "HEAD:update.command"], capture_output=True)
        self.assertEqual(shown.returncode, 0)
        with open(os.path.join(PROJECT, "update.command"), "rb") as f:
            self.assertEqual(f.read(), shown.stdout)

    def test_f4_readme_leads_with_the_button_and_discloses_the_check(self):
        text = source("README.md")
        start = text.index("### Updating from GitHub")
        end = text.index("\n### ", start + 5)
        section = text[start:end]
        self.assertIn("**Update**", section)
        self.assertLess(section.index("**Update**"), section.index("update.command"))
        self.assertIn("git init && git remote add origin https://github.com/johnalvero/gow-farm.git", section)
        start = text.index("### Connection status")
        self.assertIn("GOW_UPDATE_CHECKS", text[start:text.index("\n### ", start + 5)])


if __name__ == "__main__":
    unittest.main()
