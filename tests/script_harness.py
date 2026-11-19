"""Runs a claim script as __main__ with spies at the Playwright boundary.

Usage: script_harness.py <script> [script args...]
Reads its instructions from the JSON in GOW_TEST_HARNESS:
  spy_file               where to write launch and context counts
  raise_worker_for       emails whose worker raises before doing anything
  context_close_raises   true: BrowserContext.close closes, then raises
  context_close_hangs    true: BrowserContext.close never returns
  wedge_driver_at_context_close  true: BrowserContext.close freezes the real driver first
  browser_close_raises   true: Browser.close closes, then raises
  browser_close_hangs    true: Browser.close never returns
  wedge_driver           true: Browser.close freezes the real driver process first
  playwright_start_raises  true: async_playwright fails to start
  playwright_exit_raises   true: async_playwright stops, then raises
  playwright_exit_hangs    true: async_playwright stops, then never returns
  card_count_stable_ms     overrides claim_common.CARD_COUNT_STABLE_MS
  claim_response_timeout_ms  overrides claim_common.CLAIM_RESPONSE_TIMEOUT_MS
  goto_timeout_on_vault    true: Page.goto on the vault raises a Playwright timeout
  sign_in_timeout_seconds, session_timeout_seconds, session_request_timeout_seconds
                           override the matching claim_common constants
  login_outcome_timeout_seconds  overrides claim_common.LOGIN_OUTCOME_TIMEOUT_SECONDS
  login_dialog_grace_ms    overrides claim_common.LOGIN_DIALOG_GRACE_MS
  no_wait_for_sign_in      true: wait_for_sign_in does nothing
  no_wait_for_session      true: wait_for_session does nothing
  keep_stale_session_requests  true: vault_state's framenavigated listener never attaches
  account_deadline_seconds, account_cancel_grace_seconds, select_profile_timeout_ms
                           override the matching claim_common constants (null disables the deadline)
  select_character_true    true: select_character does its work, then always returns True
  failed_account_zeros     true: the failure result ignores finished characters
  hang_worker_after_claim  emails whose worker records a claim, then hangs
  swallow_cancel           {email: times}: the worker hangs and ignores that many cancels (-1 always)
  worker_marker            file created when a swallow_cancel worker has started
  freeze_at_inner_html     {email: n}: spin the page just before its nth inner_html call
  events_import_fails      true: importing dashboard.events raises
  events_write_raises      true: every event write raises OSError(ENOSPC)
  events_write_hangs       true: every event write hangs forever, on the writer thread
  events_sync_writes       true: events are written on the event loop, the design ISO7 rules out
  events_raw_field         true: the mock password goes past the allowlist as a label, for PW2
  account_label_identity   true: account_label returns its input, for EM3
  events_no_validate       true: the writer's allowlist is off
  max_event_bytes          overrides events.MAX_EVENT_BYTES
  default_stagger          overrides claim_common.DEFAULT_STAGGER_SECONDS
  exit_file                file the script's exit code is written to
  no_release_stdout        true: claim_common's release_stdout is unregistered
  no_say_guard             true: say() lets a stdout failure through, as print does
  stdout_enospc_after      n: stdout takes n writes, then fails with ENOSPC, as a full disk does
  cdp_hang                 new_cdp_session | send | detach: that CDP step never returns
  cdp_hang_path            only while saving a screenshot whose path contains this
  wedge_driver_at_detach   true: freeze the real driver just before CDP detach
  teardown_marker        file created when Browser.close is entered
  launch_failures        how many chromium.launch calls fail first
  dashboard_interactive  true: claim_common.launcher.interactive returns True, as from a terminal
  browser_result         what the fake webbrowser.open returns (default true)

Always, whatever the config:
  GOW_DASHBOARD_PORT must be set and must not be the real dashboard's port, or the harness refuses to run
  webbrowser.open, open_new and open_new_tab are replaced: no real browser ever opens.
                         Each URL is appended to the spy's browser_opens
  every subprocess.Popen whose argv names dashboard/server.py is recorded in the spy's
                         dashboard_servers as {pid, args}; the call itself is unchanged
"""

import asyncio
import atexit
import errno
import io
import json
import os
import runpy
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

for name in ("GOW_BASE_URL", "GOW_USERS_FILE"):
    if not os.environ.get(name):
        sys.exit(f"Refusing to run: {name} is not set")
if urlsplit(os.environ["GOW_BASE_URL"]).hostname != "127.0.0.1":
    sys.exit("Refusing to run: GOW_BASE_URL is not local")
# Unset would mean the real default port, where the user's own dashboard runs
if "GOW_DASHBOARD_PORT" not in os.environ:
    sys.exit("Refusing to run: GOW_DASHBOARD_PORT is not set")
_dashboard_port = os.environ["GOW_DASHBOARD_PORT"]
if _dashboard_port.isascii() and _dashboard_port.isdigit() and int(_dashboard_port) == 8765:
    sys.exit("Refusing to run: GOW_DASHBOARD_PORT is the real dashboard's port")

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, PROJECT)

early_config = json.loads(os.environ["GOW_TEST_HARNESS"])
if early_config.get("events_import_fails"):
    sys.modules["dashboard.events"] = None

import webbrowser  # noqa: E402

browser_opens = []
dashboard_servers = []


def fake_browser_open(url, *args, **kwargs):
    browser_opens.append(url)
    return early_config.get("browser_result", True)


webbrowser.open = fake_browser_open
webbrowser.open_new = fake_browser_open
webbrowser.open_new_tab = fake_browser_open

real_popen = subprocess.Popen


class RecordingPopen(real_popen):
    """Popen, unchanged, except that a dashboard server it starts is written down"""

    def __init__(self, args, *rest, **kwargs):
        super().__init__(args, *rest, **kwargs)
        argv = [str(arg) for arg in args] if isinstance(args, (list, tuple)) else [str(args)]
        if any(arg.replace(os.sep, "/").endswith("dashboard/server.py") for arg in argv):
            dashboard_servers.append({"pid": self.pid, "args": argv})


subprocess.Popen = RecordingPopen

import playwright.async_api as api  # noqa: E402
from playwright.async_api._context_manager import PlaywrightContextManager  # noqa: E402

import claim_common  # noqa: E402

config = json.loads(os.environ["GOW_TEST_HARNESS"])
counts = {"launches": 0, "launch_args": [], "contexts": 0, "context_closes": 0, "browser_closes": 0,
          "stable_calls": [], "routes": 0, "vault_gotos": 0, "screenshots": [],
          "sign_in_waits": [], "session_waits": [],
          "browser_opens": browser_opens, "dashboard_servers": dashboard_servers}
saving_to = None

real_launch = api.BrowserType.launch
real_new_context = api.Browser.new_context
real_context_close = api.BrowserContext.close
real_browser_close = api.Browser.close
real_run_accounts = claim_common.run_accounts
real_select_character = claim_common.select_character
real_inner_html = api.Locator.inner_html
real_wait_for_stable_count = claim_common.wait_for_stable_count
real_save_screenshot = claim_common.save_screenshot
real_wait_for_sign_in = claim_common.wait_for_sign_in
real_wait_for_session = claim_common.wait_for_session
real_route = api.BrowserContext.route
real_goto = api.Page.goto
real_new_cdp_session = api.BrowserContext.new_cdp_session
real_cdp_send = api.CDPSession.send
real_cdp_detach = api.CDPSession.detach
real_playwright_enter = PlaywrightContextManager.__aenter__
real_playwright_exit = PlaywrightContextManager.__aexit__


async def launch(self, *args, **kwargs):
    counts["launches"] += 1
    counts["launch_args"].append(kwargs.get("args"))
    if counts["launches"] <= config.get("launch_failures", 0):
        raise api.Error("forced launch failure\nsecond line of launch failure")
    return await real_launch(self, *args, **kwargs)


async def new_context(self, *args, **kwargs):
    counts["contexts"] += 1
    return await real_new_context(self, *args, **kwargs)


def freeze_driver():
    children = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True).stdout
    for pid in children.split():
        os.kill(int(pid), signal.SIGSTOP)


async def wait_for_stable_count(*args, **kwargs):
    began = time.monotonic()
    settled = await real_wait_for_stable_count(*args, **kwargs)
    counts["stable_calls"].append([time.monotonic() - began, settled])
    return settled


async def wait_for_sign_in(*args, **kwargs):
    if config.get("no_wait_for_sign_in"):
        return
    began = time.monotonic()
    await real_wait_for_sign_in(*args, **kwargs)
    counts["sign_in_waits"].append(time.monotonic() - began)


async def wait_for_session(session_requests, email):
    if config.get("no_wait_for_session"):
        return
    began = time.monotonic()
    await real_wait_for_session(session_requests, email)
    counts["session_waits"].append(time.monotonic() - began)


def cdp_step_hangs(step):
    return (config.get("cdp_hang") == step and saving_to is not None
            and config.get("cdp_hang_path", "") in saving_to)


async def save_screenshot(page, path, email):
    global saving_to
    saving_to = path
    began = time.monotonic()
    await real_save_screenshot(page, path, email)
    saving_to = None
    written = os.stat(path).st_mtime_ns if os.path.exists(path) else None
    counts["screenshots"].append([path, written, time.monotonic() - began])


async def new_cdp_session(self, *args, **kwargs):
    if cdp_step_hangs("new_cdp_session"):
        await asyncio.Event().wait()
    return await real_new_cdp_session(self, *args, **kwargs)


async def cdp_send(self, *args, **kwargs):
    if cdp_step_hangs("send"):
        await asyncio.Event().wait()
    return await real_cdp_send(self, *args, **kwargs)


async def cdp_detach(self, *args, **kwargs):
    if cdp_step_hangs("detach"):
        await asyncio.Event().wait()
    if config.get("wedge_driver_at_detach") and saving_to and "_claim_failed" in saving_to:
        freeze_driver()
    return await real_cdp_detach(self, *args, **kwargs)


async def context_route(self, *args, **kwargs):
    counts["routes"] += 1
    return await real_route(self, *args, **kwargs)


real_login = claim_common.login


async def login(page, email, *args, **kwargs):
    page.gow_email = email
    return await real_login(page, email, *args, **kwargs)


async def page_goto(self, url, *args, **kwargs):
    if url.endswith("/catalog/main"):
        counts["vault_gotos"] += 1
        if config.get("goto_timeout_on_vault"):
            raise api.TimeoutError("Page.goto: Timeout 60000ms exceeded.\nCall log:\n  - navigating")
    return await real_goto(self, url, *args, **kwargs)


async def context_close(self, *args, **kwargs):
    counts["context_closes"] += 1
    if config.get("wedge_driver_at_context_close"):
        freeze_driver()
    if config.get("context_close_hangs"):
        await asyncio.Event().wait()
    await real_context_close(self, *args, **kwargs)
    if config.get("context_close_raises"):
        raise RuntimeError("forced context close failure\nsecond line of close failure")


async def browser_close(self, *args, **kwargs):
    counts["browser_closes"] += 1
    if config.get("teardown_marker"):
        with open(config["teardown_marker"], "w"):
            pass
    if config.get("wedge_driver"):
        freeze_driver()
    if config.get("browser_close_hangs"):
        await asyncio.Event().wait()
    await real_browser_close(self, *args, **kwargs)
    if config.get("browser_close_raises"):
        raise RuntimeError("forced browser close failure\nsecond line of browser close failure")


async def playwright_enter(self):
    if config.get("playwright_start_raises"):
        raise RuntimeError("forced playwright start failure\nsecond line of playwright start failure")
    return await real_playwright_enter(self)


async def playwright_exit(self, *args):
    await real_playwright_exit(self, *args)
    if config.get("playwright_exit_raises"):
        raise RuntimeError("forced playwright stop failure\nsecond line of playwright stop failure")
    if config.get("playwright_exit_hangs"):
        await asyncio.Event().wait()


async def run_accounts(accounts, worker, failure_result, options):
    async def stubborn(times):
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if times == 0:
                    raise
                times -= 1

    async def wrapped(browsers, account, account_num, total):
        email = account["email"]
        if email in config.get("raise_worker_for", []):
            raise RuntimeError("forced worker failure\nsecond line of worker failure")
        if email in config.get("hang_worker_after_claim", []):
            account["outcome"] = claim_common.CLAIMED
            await asyncio.Event().wait()
        if email in config.get("swallow_cancel", {}):
            if config.get("worker_marker"):
                with open(config["worker_marker"], "w"):
                    pass
            await stubborn(config["swallow_cancel"][email])
        return await worker(browsers, account, account_num, total)

    def zeros(account):
        return {"total": 0, "claimed": 0, "already": 0}

    chosen = zeros if config.get("failed_account_zeros") else failure_result
    return await real_run_accounts(accounts, wrapped, chosen, options)


async def select_character(*args, **kwargs):
    selected = await real_select_character(*args, **kwargs)
    return True if config.get("select_character_true") else selected


inner_html_calls = {}


async def inner_html(self, *args, **kwargs):
    for email, nth in config.get("freeze_at_inner_html", {}).items():
        page = self.page
        if getattr(page, "gow_email", None) == email:
            inner_html_calls[email] = inner_html_calls.get(email, 0) + 1
            if inner_html_calls[email] == nth:
                await page.evaluate("setTimeout(() => { while (true) {} }, 0)")
                await asyncio.sleep(0.2)
    return await real_inner_html(self, *args, **kwargs)


def write_counts():
    counts["network_loaded"] = "dashboard.network" in sys.modules
    with open(config["spy_file"], "w") as f:
        json.dump(counts, f)


api.BrowserType.launch = launch
api.Browser.new_context = new_context
api.BrowserContext.close = context_close
api.Browser.close = browser_close
PlaywrightContextManager.__aenter__ = playwright_enter
PlaywrightContextManager.__aexit__ = playwright_exit
claim_common.run_accounts = run_accounts
claim_common.wait_for_stable_count = wait_for_stable_count
claim_common.save_screenshot = save_screenshot
claim_common.wait_for_sign_in = wait_for_sign_in
claim_common.wait_for_session = wait_for_session
claim_common.select_character = select_character
claim_common.login = login
api.Locator.inner_html = inner_html
for key, name in (("sign_in_timeout_seconds", "SIGN_IN_TIMEOUT_SECONDS"),
                  ("session_timeout_seconds", "SESSION_TIMEOUT_SECONDS"),
                  ("session_request_timeout_seconds", "SESSION_REQUEST_TIMEOUT_SECONDS"),
                  ("account_deadline_seconds", "ACCOUNT_DEADLINE_SECONDS"),
                  ("account_cancel_grace_seconds", "ACCOUNT_CANCEL_GRACE_SECONDS"),
                  ("login_outcome_timeout_seconds", "LOGIN_OUTCOME_TIMEOUT_SECONDS"),
                  ("login_dialog_grace_ms", "LOGIN_DIALOG_GRACE_MS")):
    if key in config:
        setattr(claim_common, name, config[key])
if config.get("keep_stale_session_requests"):
    real_page_on = api.Page.on
    real_page_remove = api.Page.remove_listener

    def page_on(self, event, handler):
        if event != "framenavigated":
            real_page_on(self, event, handler)

    def page_remove_listener(self, event, handler):
        if event != "framenavigated":
            real_page_remove(self, event, handler)

    api.Page.on = page_on
    api.Page.remove_listener = page_remove_listener

api.BrowserContext.route = context_route
api.Page.goto = page_goto
api.BrowserContext.new_cdp_session = new_cdp_session
api.CDPSession.send = cdp_send
api.CDPSession.detach = cdp_detach
if "select_profile_timeout_ms" in config:
    claim_common.SELECT_PROFILE_TIMEOUT_MS = config["select_profile_timeout_ms"]
if "claim_response_timeout_ms" in config:
    claim_common.CLAIM_RESPONSE_TIMEOUT_MS = config["claim_response_timeout_ms"]
if "card_count_stable_ms" in config:
    claim_common.CARD_COUNT_STABLE_MS = config["card_count_stable_ms"]
atexit.register(write_counts)

events = claim_common.events


def no_space(fd, data):
    raise OSError(errno.ENOSPC, "No space left on device")


def hang(fd, data):
    threading.Event().wait()


def hang_here(self, name, **fields):
    threading.Event().wait()


if config.get("events_write_raises"):
    events.write_line = no_space
if config.get("events_write_hangs"):
    events.write_line = hang
if config.get("events_sync_writes"):
    events.Recorder.put = hang_here
if config.get("events_raw_field"):
    from mock_site import PASSWORD

    events.validate_event = lambda event: True
    events.account_label = lambda email: PASSWORD
if config.get("account_label_identity"):
    events.account_label = lambda email: email
if config.get("events_no_validate"):
    events.validate_event = lambda event: True
if config.get("no_release_stdout"):
    atexit.unregister(claim_common.release_stdout)
if config.get("no_say_guard"):
    claim_common.say = lambda text: print(text, flush=True)
if "stdout_enospc_after" in config:
    class FullDisk(io.RawIOBase):
        """Fails like a full disk until release_stdout points fd 1 at /dev/null"""

        def __init__(self, allowed):
            super().__init__()
            self.allowed = allowed

        def writable(self):
            return True

        def fileno(self):
            return 1

        def write(self, data):
            if self.allowed <= 0 and not stat.S_ISCHR(os.fstat(1).st_mode):
                raise OSError(errno.ENOSPC, "No space left on device")
            self.allowed -= 1
            return os.write(1, bytes(data))

    sys.stdout = io.TextIOWrapper(io.BufferedWriter(FullDisk(config["stdout_enospc_after"])), encoding="utf-8")
if "max_event_bytes" in config:
    events.MAX_EVENT_BYTES = config["max_event_bytes"]
if "default_stagger" in config:
    claim_common.DEFAULT_STAGGER_SECONDS = config["default_stagger"]
if config.get("dashboard_interactive"):
    claim_common.launcher.interactive = lambda: True

script = os.path.join(PROJECT, sys.argv[1])
sys.argv = [script] + sys.argv[2:]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as stopped:
    if config.get("exit_file"):
        with open(config["exit_file"], "w") as f:
            f.write(str(stopped.code))
    raise
