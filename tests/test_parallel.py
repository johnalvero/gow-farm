"""Parallelism, pacing, isolation and argument handling, end to end."""

import unittest

from gow_support import ALL_SCRIPTS, Account, GowTestCase, range_args, users_text

STAGGER = "--stagger"


def emails(count, tag="par"):
    return [f"{tag}{i}@x.test" for i in range(1, count + 1)]


class Peak(GowTestCase):
    """Peak is measured at the mock: browser contexts overlapping in time,
    each from its first request to the last poll its page sent."""

    def peak_of(self, count, *args):
        addresses = emails(count)
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", str(count), *args)
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(len(mock.ledger_pairs()), count)
        return mock.peak_concurrency()

    def test_concurrency_2_with_6_accounts(self):
        self.assertEqual(self.peak_of(6, "--concurrency", "2", STAGGER, "0.2"), 2)

    def test_default_concurrency_flag_absent(self):
        self.assertEqual(self.peak_of(6, STAGGER, "0.2"), 3)

    def test_concurrency_1(self):
        self.assertEqual(self.peak_of(3, "--concurrency", "1", STAGGER, "0.2"), 1)

    def test_no_flags_at_all_gives_peak_3_at_10s_spacing(self):
        """Accounts held open past 20s so the default 10s stagger can reach the default limit"""
        addresses = emails(4)
        mock = self.make_mock({e: Account(vault_delay_ms=18000) for e in addresses}, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "4")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(mock.peak_concurrency(), 3)
        gaps = [b - a for a, b in zip(mock.login_hits, mock.login_hits[1:])]
        self.assertGreaterEqual(min(gaps), 9.5, gaps)

    def test_concurrency_5_is_under_half_the_wall_time_of_1(self):
        addresses = emails(5)
        accounts = {e: Account(vault_delay_ms=2000) for e in addresses}
        times = {}
        for concurrency in ("1", "5"):
            mock = self.make_mock(accounts, hydrate_ms=100)
            result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "5",
                                  "--concurrency", concurrency, STAGGER, "0")
            self.assertEqual(result.returncode, 0, result.output[-1500:])
            times[concurrency] = result.seconds
        self.assertLess(times["5"], times["1"] / 2, times)


class Stagger(GowTestCase):
    def test_half_second_stagger_holds_across_the_whole_run(self):
        """401 accounts finish at once, so slots free up well inside the stagger"""
        addresses = emails(6)
        accounts = {e: Account(password="other") for e in addresses[::2]}
        mock = self.make_mock(accounts, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "6",
                              "--concurrency", "2", STAGGER, "0.5")
        gaps = [b - a for a, b in zip(mock.login_hits, mock.login_hits[1:])]
        self.assertEqual(len(gaps), 5, result.output[-1500:])
        self.assertGreaterEqual(min(gaps), 0.45, gaps)

    def test_stagger_binds_for_accounts_started_after_a_finish(self):
        """At concurrency 2 a slot frees at about 5.5s, inside the 6s the third start is due"""
        addresses = emails(4)
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "4",
                              "--concurrency", "2", STAGGER, "3")
        gaps = [b - a for a, b in zip(mock.login_hits, mock.login_hits[1:])]
        self.assertEqual(len(gaps), 3, result.output[-1500:])
        self.assertGreaterEqual(min(gaps), 2.95, gaps)


class Isolation(GowTestCase):
    def test_one_browser_and_one_context_per_account(self):
        addresses = emails(5)
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "5",
                              STAGGER, "0.1", harness={})
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(result.spy["launches"], 1)
        self.assertEqual(result.spy["contexts"], 5)
        self.assertEqual(result.spy["context_closes"], 5)
        self.assertEqual(result.spy["launch_args"], [["--no-sandbox", "--disable-setuid-sandbox"]])

    def test_no_cookie_on_first_request_and_ledger_matches_each_login(self):
        addresses = emails(5)
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text(addresses), "1", "5",
                              "--concurrency", "3", STAGGER, "0.1")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(mock.login_page_cookies, [None] * 5)
        self.assertEqual(len(mock.visitors), 5)
        self.assertEqual(sorted(c.email for c in mock.ledger), addresses)
        for claim in mock.ledger:
            with self.subTest(claim.email):
                self.assertEqual(mock.visitors[claim.visitor].email, claim.email)

    def test_cookies_never_cross_accounts_at_concurrency_3(self):
        addresses = emails(6)
        accounts = {e: Account(characters=("c1", "c2")) for e in addresses[:3]}
        mock = self.make_mock(accounts, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text(addresses), "1", "6",
                              "--concurrency", "3", STAGGER, "0.1")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(len(mock.acct_cookies), 6)
        self.assertEqual(mock.cross_account, [])


class LineSelection(GowTestCase):
    FIVE = emails(5, "line")
    SELECTED = sorted(FIVE[1:4])

    def selected_by(self, script, *args):
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow(script, mock, users_text(self.FIVE), *args)
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        return sorted(e for e, _ in mock.ledger_pairs())

    def test_flags_select_the_same_lines_as_positional_only(self):
        forms = (
            ("claim_all_characters.py", "2", "4"),
            ("claim_all_characters.py", "2", "4", "--concurrency", "2"),
            ("claim_all_characters.py", "--concurrency=2", "2", "4"),
            ("claim_batch.py", "2", "4"),
            ("claim_batch.py", "2", "4", STAGGER, "0"),
        )
        results = self.run_all([lambda f=form: self.selected_by(*f) for form in forms])
        for form, selected in zip(forms, results):
            with self.subTest(form):
                self.assertEqual(selected, self.SELECTED)

    def test_no_range_processes_every_account(self):
        addresses = emails(4, "all")
        mock = self.make_mock(hydrate_ms=100)
        text = "# comment\n\n" + users_text(addresses)
        result = self.run_gow("claim_all_characters.py", mock, text, STAGGER, "0")
        self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), addresses, result.output[-1500:])

    def test_claim_all_still_ignores_extra_positionals(self):
        addresses = emails(2, "extra")
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_all.py", mock, users_text(addresses), "9", "9", STAGGER, "0")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), addresses)


class MistypedFlags(GowTestCase):
    def run_args(self, args_by_script):
        mock = self.make_mock()
        calls = [(script, args) for script in ALL_SCRIPTS for args in args_by_script]
        results = self.run_all([
            lambda s=script, a=args: self.run_gow(s, mock, users_text(["cli@x.test"]), *a, harness={})
            for script, args in calls])
        return mock, zip(calls, results)

    def test_unknown_flags_exit_1_before_any_browser_or_request(self):
        cases = {("--concurency", "1"): "--concurency", ("--concurency=1",): "--concurency", ("--bogus",): "--bogus"}
        mock, runs = self.run_args(list(cases))
        for (script, args), result in runs:
            with self.subTest(script, args=args):
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stdout, rf"(?m)^Error: unknown option {cases[args]}$")
                self.assertEqual(result.spy["launches"], 0)
        self.assertEqual(mock.total_requests, 0)

    def test_a_value_on_a_mistyped_flag_is_never_echoed(self):
        mock, runs = self.run_args([("--password=SECRET",), ("--password", "SECRET")])
        for (script, args), result in runs:
            with self.subTest(script, args=args):
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("SECRET", result.output)

    def test_help_prints_usage_and_exits_0_with_no_browser(self):
        mock, runs = self.run_args([("--help",)])
        for (script, args), result in runs:
            with self.subTest(script):
                self.assertEqual(result.returncode, 0)
                usage = next(l for l in result.stdout.splitlines() if l.startswith("Usage:"))
                self.assertIn("--concurrency", usage)
                self.assertIn("--stagger", usage)
                self.assertIn("--no-block", result.stdout)
                self.assertIn("--force-concurrency", result.stdout)
                self.assertEqual(result.spy["launches"], 0)
        self.assertEqual(mock.total_requests, 0)

    def test_claim_all_treats_a_stray_dash_argument_as_a_mistyped_flag(self):
        mock = self.make_mock()
        result = self.run_gow("claim_all.py", mock, users_text(["cli@x.test"]), "-5", harness={})
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"(?m)^Error: unknown option -5$")
        self.assertEqual(result.spy["launches"], 0)
        self.assertEqual(mock.total_requests, 0)

    def test_claim_all_short_help(self):
        mock = self.make_mock()
        result = self.run_gow("claim_all.py", mock, users_text(["cli@x.test"]), "-h", harness={})
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"(?m)^Usage: python claim_all.py")
        self.assertEqual(result.spy["launches"], 0)

    def test_claim_all_ignores_a_bare_positional_before_a_flag(self):
        addresses = emails(2, "bare")
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_all.py", mock, users_text(addresses), "extra", STAGGER, "0")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), addresses)

    def test_negative_end_line_still_reaches_the_range_check(self):
        mock = self.make_mock()
        result = self.run_gow("claim_batch.py", mock, users_text(["cli@x.test"]), "1", "-5", harness={})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error: Invalid line range", result.stdout)
        self.assertEqual(result.spy["launches"], 0)


class NoBlock(GowTestCase):
    def test_images_and_analytics_reach_the_server_and_a_claim_lands(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({"open@x.test": Account()}, hydrate_ms=100), users_text(["open@x.test"]),
                [*range_args(s, 1), STAGGER, "0", "--no-block"], {"harness": {}})):
            with self.subTest(script):
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertEqual(mock.ledger_pairs(), [("open@x.test", "only")])
                self.assertGreater(mock.image_hits, 0)
                self.assertGreater(mock.analytics_hits, 0)
                self.assertEqual(result.spy["routes"], 0)

    def test_multi_character_flow_works_unblocked(self):
        email = "openmulti@x.test"
        mock = self.make_mock({email: Account(characters=("c1", "c2"))}, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text([email]), "1", "1", STAGGER, "0", "--no-block")
        self.assertIn("Claimed 2/2 characters", result.stdout, result.output[-2000:])
        self.assertGreater(mock.image_hits, 0)

    def test_warning_comes_before_the_banner_and_only_with_the_flag(self):
        for script, _, result in self.for_each_script(lambda s: (
                self.make_mock(hydrate_ms=100), users_text(["banner@x.test"]),
                [*range_args(s, 1), STAGGER, "0", "--no-block"], {})):
            with self.subTest(script):
                lines = result.stdout.splitlines()
                warning = next(i for i, l in enumerate(lines) if "Resource blocking disabled (--no-block)" in l)
                banner = next(i for i, l in enumerate(lines) if "=" * 60 in l)
                self.assertLess(warning, banner)
                self.assert_tagged(result, ["banner@x.test"])

    def test_default_run_blocks_and_says_nothing(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(hydrate_ms=100), users_text(["shut@x.test"]),
                [*range_args(s, 1), STAGGER, "0"], {"harness": {}})):
            with self.subTest(script):
                self.assertNotIn("Resource blocking disabled", result.stdout)
                self.assertEqual(mock.image_hits + mock.analytics_hits + mock.font_hits, 0)
                self.assertEqual(result.spy["routes"], result.spy["contexts"])

    def test_mistyped_flag_launches_nothing(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(), users_text(["cli@x.test"]), ["--no-blok"], {"harness": {}})):
            with self.subTest(script):
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stdout, r"(?m)^Error: unknown option --no-blok$")
                self.assertEqual(result.spy["launches"], 0)
                self.assertEqual(mock.total_requests, 0)

    def test_line_selection_is_the_same_with_the_flag(self):
        five = emails(5, "nobl")
        mock = self.make_mock(hydrate_ms=100)
        self.run_gow("claim_all_characters.py", mock, users_text(five), "2", "4", "--concurrency", "2",
                     STAGGER, "0", "--no-block")
        self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), sorted(five[1:4]))


class ConcurrencyCeiling(GowTestCase):
    def test_5_runs(self):
        addresses = emails(5, "five")
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "5", "--concurrency", "5", STAGGER, "0.1")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(mock.peak_concurrency(), 5)

    def test_above_5_is_refused_before_anything_starts(self):
        cases = [(script, value) for script in ALL_SCRIPTS for value in ("6", "10")]
        mock = self.make_mock()
        results = self.run_all([
            lambda s=script, v=value: self.run_gow(s, mock, users_text(["cli@x.test"]), "--concurrency", v, harness={})
            for script, value in cases])
        for (script, value), result in zip(cases, results):
            with self.subTest(script, value=value):
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stdout, r"(?m)^Error: --concurrency above 5 needs --force-concurrency$")
                self.assertEqual(result.spy["launches"], 0)
        self.assertEqual(mock.total_requests, 0)

    def test_6_with_the_flag_reaches_peak_6_and_warns(self):
        addresses = emails(6, "six")
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "6",
                              "--concurrency", "6", "--force-concurrency", STAGGER, "0.1")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(mock.peak_concurrency(), 6)
        self.assertRegex(result.stdout, r"\[WARNING\] .*concurrency 6")

    def test_force_alone_stays_at_the_default(self):
        addresses = emails(6, "forc")
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "6", "--force-concurrency", STAGGER, "0.1")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertEqual(mock.peak_concurrency(), 3)


class BadArguments(GowTestCase):
    def test_bad_option_values_exit_before_any_browser_or_request(self):
        cases = [(script, value) for script in ALL_SCRIPTS
                 for value in (("--concurrency", "0"), ("--concurrency", "abc"), (STAGGER, "-1"))]
        mock = self.make_mock()
        results = self.run_all([
            lambda s=script, v=value: self.run_gow(s, mock, users_text(["cli@x.test"]), *v, harness={})
            for script, value in cases])
        for (script, value), result in zip(cases, results):
            with self.subTest(script, value=value):
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stdout, r"(?m)^Error: ")
                self.assertEqual(result.spy["launches"], 0)
        self.assertEqual(mock.total_requests, 0)

    def test_existing_messages_and_exit_codes_are_kept(self):
        cases = (
            ("claim_all_characters.py", ("1",), "Usage: python claim_all_characters.py"),
            ("claim_all_characters.py", ("1", "2", "3"), "Usage: python claim_all_characters.py"),
            ("claim_all_characters.py", ("a", "b"), "Error: start_line and end_line must be integers"),
            ("claim_batch.py", (), "Usage: python claim_batch.py"),
            ("claim_batch.py", ("1",), "Usage: python claim_batch.py"),
            ("claim_batch.py", ("a", "b"), "Error: start_line and end_line must be integers"),
            ("claim_batch.py", ("5", "2"), "Error: Invalid line range"),
            ("claim_batch.py", ("0", "2"), "Error: Invalid line range"),
            ("claim_batch.py", ("-1", "2"), "Error: Invalid line range"),
        )
        mock = self.make_mock()
        results = self.run_all([
            lambda s=script, a=args: self.run_gow(s, mock, users_text(["cli@x.test"]), *a, harness={})
            for script, args, _ in cases])
        for (script, args, message), result in zip(cases, results):
            with self.subTest(script, args=args):
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stdout)
                self.assertEqual(result.spy["launches"], 0)
        self.assertEqual(mock.total_requests, 0)


if __name__ == "__main__":
    unittest.main()
