#!/usr/bin/env python3
"""
Game of War - Shared claim helpers
Login, vault checks, resource blocking and the parallel account runner used by
claim_all_characters.py, claim_all.py and claim_batch.py
"""

import argparse
import asyncio
import atexit
import base64
import contextlib
import json
import math
import os
import re
import string
import sys
import time
import traceback
from datetime import datetime
from urllib.parse import quote, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

class NoEvents:
    """Stands in for the event recorder when dashboard/ is missing or broken"""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

# Guarded, because a broken dashboard must never stop a claim
try:
    from dashboard import events
except Exception:
    events = NoEvents()

try:
    from dashboard import launcher
except Exception:
    launcher = NoEvents()

BASE_URL = os.environ.get("GOW_BASE_URL", "https://www.gameofwar-fireage.com/en-us")
LOGIN_URL = f"{BASE_URL}/login"
VAULT_URL = f"{BASE_URL}/catalog/main"

LOGIN_API_PATH = "/api/account/login"
SELECT_PROFILE_API_PATH = "/api/account/select-profile"
CLAIM_API_PATH = "/api/appcharge/collect-offer"

DEFAULT_CONCURRENCY = 3
DEFAULT_STAGGER_SECONDS = 10
MAX_CONCURRENCY = 5

HYDRATION_TIMEOUT_MS = 60000
VAULT_TIMEOUT_MS = 30000
CLAIM_REQUEST_TIMEOUT_MS = 10000
CLAIM_RESPONSE_TIMEOUT_MS = 30000
CLAIM_BODY_TIMEOUT_SECONDS = 5
CLAIM_CONSOLE_SETTLE_SECONDS = 0.5
CLAIM_SCREENSHOT_TIMEOUT_SECONDS = 10
SESSION_API_PATH = "/api/auth/session"
PROVIDER_TOKEN_API_PATH = "/api/appcharge/get-token"
AUTH_CALLBACK_API_PATH = "/api/auth/callback/credentials"
SIGN_IN_TIMEOUT_SECONDS = 30
SIGN_IN_START_TIMEOUT_SECONDS = 3
SESSION_SETTLE_SECONDS = 0.5
SESSION_REQUEST_TIMEOUT_SECONDS = 5
SESSION_TIMEOUT_SECONDS = 20
CLAIM_CONSOLE_LIMIT = 5
CLAIM_NETWORK_LIMIT = 12
VAULT_RETRY_DELAY_SECONDS = 2
LOGIN_RETRY_BACKOFF_SECONDS = 5
LOGIN_RESPONSE_TIMEOUT_MS = 30000
# Counted from the login response: the real dialog has taken up to 11s
LOGIN_OUTCOME_TIMEOUT_SECONDS = 30
LOGIN_OUTCOME_POLL_MS = 250
# How long a dialog may still appear after a sign-in that showed none
LOGIN_DIALOG_GRACE_MS = 3000
SELECT_PROFILE_TIMEOUT_MS = 15000
SELECTED_PROFILE_TIMEOUT_MS = 3000
CARD_COUNT_STABLE_MS = 1500
CARD_COUNT_TIMEOUT_MS = 8000
CONTEXT_CLOSE_TIMEOUT_SECONDS = 30
BROWSER_CLOSE_TIMEOUT_SECONDS = 30
PLAYWRIGHT_STOP_TIMEOUT_SECONDS = 5
ACCOUNT_DEADLINE_SECONDS = 900
# Must stay above CONTEXT_CLOSE_TIMEOUT_SECONDS, so a stopping account can close
ACCOUNT_CANCEL_GRACE_SECONDS = 35

CLAIMED = "claimed"
ALREADY_CLAIMED = "already_claimed"
FAILED = "failed"
JEWELS_PER_CLAIM = 75

BLOCKED_HOSTS = ("googletagmanager.com", "google-analytics.com")
ANALYTICS_TUNNEL_PATH = "/api/tunnel/analytics/"
TRANSPARENT_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# Worker tasks still running, which exit_if_stuck checks once the run is over
running_workers = set()

IS_HYDRATED_JS = "el => Object.keys(el).some(key => key.startsWith('__reactProps'))"

# These run on the event loop every account shares, so none may backtrack over
# a run from each of its characters: LONG_RUN_RE matches whole runs and leaves
# the length check to sanitize_detail, EMAIL_RE's second branch consumes a run
# that holds no email, and the key patterns start at the keyword, not at a
# prefix they would only put back. One failed claim sanitizes up to 17 lines in
# a row.
LONG_RUN_RE = re.compile(r"[\w.+%-]+")
LONG_RUN_MIN = 256
EMAIL_RE = re.compile(r"([\w.+%-]+(?:@|%40)[\w.-]+)|[\w.+%-]+")
# The whole rest of the line: "Bearer abc" and "a=b; c=d" hold several tokens
HEADER_VALUE_RE = re.compile(r"""(?i)((?:cookie|authorization)[\w-]*["']?\s*[:=]\s*).*""")
KEY_VALUE_RE = re.compile(
    r"""(?i)((?:password|passwd|token|secret|session|user_id|useremail)[\w-]*["']?\s*[:=]\s*)"""
    r"""(?:"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|[^\s,;&}\])]+)"""
)
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/=.%]{32,}")
# What a cut can leave half of: anything TOKEN_RE would match, and email addresses
CUT_TAIL_CHARS = string.ascii_letters + string.digits + "_-+/=.%@"
# Far above the output limit, and small enough to run the patterns on the event
# loop every account shares
MAX_SANITIZE_INPUT = 8192

def say(text):
    """Print a line - a full disk or a closed pipe must never stop a claim"""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass

def log(msg, level="INFO"):
    """Print timestamped log message"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    say(f"[{timestamp}] [{level}] {msg}")

def release_stdout():
    """Drop any output stdout can't take, so the run still exits with its own code"""
    # A failed print leaves its line buffered, and Python's own final flush would
    # then fail too, reporting an error and turning any exit code into 120
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass

atexit.register(release_stdout)

def first_line(e):
    """First line of an exception - Playwright messages are multi-line, and later lines would lose the email prefix"""
    lines = str(e).strip().splitlines()
    return lines[0] if lines else type(e).__name__

def cap_input(text):
    """Cut text to what the patterns may see, dropping the whole run the cut lands in"""
    # A fixed-size tail wouldn't do: a long token could leave a piece before it
    # that is too short for TOKEN_RE, and earlier redaction can bring that piece
    # within the output limit
    if len(text) <= MAX_SANITIZE_INPUT:
        return text
    return text[:MAX_SANITIZE_INPUT].rstrip(CUT_TAIL_CHARS) + "<redacted>"

def sanitize_detail(text, secrets=(), limit=200):
    """Reduce text from the page or the API to one line that is safe to log"""
    try:
        detail = text if isinstance(text, str) else str(text)

        for secret in secrets:
            if secret:
                detail = detail.replace(secret, "<redacted>")

        lines = [line for line in detail.splitlines() if line.strip()]
        detail = cap_input(lines[0].strip()) if lines else ""

        detail = LONG_RUN_RE.sub(lambda run: "<redacted>" if len(run[0]) >= LONG_RUN_MIN else run[0], detail)
        detail = EMAIL_RE.sub(lambda match: "<email>" if match[1] else match[0], detail)
        detail = HEADER_VALUE_RE.sub(r"\1<redacted>", detail)
        detail = KEY_VALUE_RE.sub(r"\1<redacted>", detail)
        detail = TOKEN_RE.sub("<redacted>", detail)

        # Last, so a cut can't leave part of a token behind
        return detail[:limit]
    except Exception:
        return "<unprintable>"

def sanitize_path(target, secrets=(), limit=200):
    """Sanitize a host and path one segment at a time, so only a token-like segment is lost"""
    # As one string, TOKEN_RE reads any long path as a single token
    try:
        for secret in secrets:
            if secret:
                # On the whole path first: a secret can contain a slash
                target = target.replace(secret, "<redacted>").replace(quote(secret, safe=""), "<redacted>")

        segments = []
        for segment in cap_input(target).split("/"):
            segments.append(sanitize_detail(segment))
            if sum(len(part) + 1 for part in segments) > limit:
                break

        return "/".join(segments)[:limit]
    except Exception:
        return "<unprintable>"

def js_truthy(value):
    """JavaScript truthiness, which unlike Python's counts empty objects and arrays as true"""
    return value not in (None, False, 0, "")

def log_traceback(email, e):
    """Log a traceback line by line under the account's email"""
    # Built from the frames, not format_exc(): Playwright's call log quotes the
    # value given to fill(), so the full message can contain the password
    for frame in traceback.format_tb(e.__traceback__):
        for line in frame.splitlines():
            log(f"[{email}] {line}", "ERROR")
    log(f"[{email}] {type(e).__name__}: {first_line(e)}", "ERROR")

def safe_exception_handler(loop, context):
    """Report unretrieved asyncio exceptions without their details"""
    # On interrupt, a pending fill() fails on a Playwright-internal future nobody
    # retrieves, and repr() of that future carries the password. So never output
    # the context's message, future or task, or the default handler that does.
    # Raising here would hand the context to that default handler, and this can
    # run after the loop has closed, so it must not use the loop either.
    try:
        exc = context.get("exception")
        if exc is None:
            log("Background error", "WARNING")
        else:
            log(f"Background error: {type(exc).__name__}: {first_line(exc)}", "WARNING")
    except Exception:
        pass

class RunOptionsParser(argparse.ArgumentParser):
    """Argument parser that reports bad values the way the scripts report bad arguments"""

    def error(self, message):
        print(f"Error: {message}")
        sys.exit(1)

def concurrency_value(text):
    """Parse --concurrency: an integer of at least 1"""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got {text!r}")
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value

def stagger_value(text):
    """Parse --stagger: a finite number of seconds, zero or more"""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {text!r}")
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"must be zero or more, got {text}")
    return value

def lines_value(text):
    """Parse --lines: comma-separated line numbers, returned sorted with no repeats"""
    if not re.fullmatch(r"[1-9][0-9]{0,6}(,[1-9][0-9]{0,6})*", text):
        raise argparse.ArgumentTypeError("must be line numbers separated by commas")
    return sorted({int(number) for number in text.split(",")})

def parse_run_options(argv, usage="", allow_lines=False):
    """Split --concurrency/--stagger out of argv, returning (options, leftover args)"""
    parser = RunOptionsParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--concurrency", type=concurrency_value, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--stagger", type=stagger_value, default=DEFAULT_STAGGER_SECONDS)
    parser.add_argument("--no-block", action="store_true")
    parser.add_argument("--force-concurrency", action="store_true")
    parser.add_argument("--no-dashboard", action="store_true")
    if allow_lines:
        parser.add_argument("--lines", type=lines_value)
    options, leftovers = parser.parse_known_args(argv)

    if "--help" in leftovers:
        print(usage)
        sys.exit(0)

    # Single-dash values such as -5 stay: the scripts' range checks handle them
    for arg in leftovers:
        if arg.startswith("--"):
            # Only the name, so a mistyped --password=... can't echo its value
            print(f"Error: unknown option {arg.split('=', 1)[0]}")
            sys.exit(1)

    if options.concurrency > MAX_CONCURRENCY:
        if not options.force_concurrency:
            print(f"Error: --concurrency above {MAX_CONCURRENCY} needs --force-concurrency")
            sys.exit(1)
        log(f"Running at concurrency {options.concurrency}, above the usual limit of {MAX_CONCURRENCY}", "WARNING")

    if options.no_block:
        log("Resource blocking disabled (--no-block)", "WARNING")

    return options, leftovers

def is_valid_email_field(email):
    """False if the field could hold part of a password - it would go on to tag every log line"""
    if re.search(r"[\s,;]", email):
        return False
    return "@" not in email or bool(re.fullmatch(r"[A-Za-z0-9.-]+", email.rsplit("@", 1)[1]))

def is_blocked_host(host):
    """True for a blocked host or any of its subdomains"""
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS)

async def block_nonessential(route):
    """Skip what the claim flow doesn't need - fonts stall the page load, the rest is just weight"""
    request = route.request
    url = urlsplit(request.url)

    try:
        if is_blocked_host((url.hostname or "").lower()):
            await route.abort()
        elif ANALYTICS_TUNNEL_PATH in url.path:
            await route.fulfill(status=200, content_type="application/json", body="{}")
        elif request.resource_type in ("font", "media"):
            await route.abort()
        elif request.resource_type == "image":
            # Fulfil rather than abort: a component gated on image load drops its
            # <img> on abort, and the flow selects on img[src*="chat_avatar"] and
            # alt="Total Power". A real 1x1 fires onload and leaves the DOM as is.
            await route.fulfill(
                status=200,
                content_type="image/gif",
                headers={"Access-Control-Allow-Origin": "*"},
                body=TRANSPARENT_GIF
            )
        else:
            await route.continue_()
    except PlaywrightError:
        # The context closed with this request in flight
        pass

class BrowserHolder:
    """One shared browser, launched on first use and relaunched if it crashes"""

    def __init__(self, playwright, pace=None, block_resources=True):
        self._playwright = playwright
        self._browser = None
        self._lock = asyncio.Lock()
        self.pace = pace
        self.block_resources = block_resources

    async def get(self):
        """Return a connected browser"""
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox']
                )
            return self._browser

    async def close(self):
        """Close the browser if it is still running"""
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                await self._browser.close()

@contextlib.asynccontextmanager
async def account_page(browsers, email):
    """Yield a page in a new isolated context, closing the context afterwards"""
    browser = await browsers.get()
    context = await browser.new_context()

    try:
        if browsers.block_resources:
            await context.route("**/*", block_nonessential)
        yield await context.new_page()
    finally:
        try:
            await asyncio.wait_for(context.close(), CONTEXT_CLOSE_TIMEOUT_SECONDS)
        except Exception as e:
            log(f"[{email}] Error closing browser context: {first_line(e)}", "WARNING")

async def close_browser(browsers):
    """Close the shared browser - a failed or hung close must not cost the finished results"""
    try:
        await asyncio.wait_for(browsers.close(), BROWSER_CLOSE_TIMEOUT_SECONDS)
    except Exception as e:
        log(f"Error closing browser: {first_line(e)}", "WARNING")

def kill_driver(manager):
    """Kill Playwright's driver process if it is still running"""
    # Private attributes: Playwright exposes no handle on this process. A frozen
    # driver holds our stderr open, so whoever reads the output would never see
    # EOF. The browser exits by itself once the driver's pipe closes.
    try:
        manager._connection._transport._proc.kill()
    except (AttributeError, ProcessLookupError):
        pass

async def stop_playwright(manager):
    """Stop Playwright - a failed or hung stop must not cost the results or replace an interrupt"""
    # Shielded because cancelling the stop would cancel a future that Playwright's
    # transport still completes once the driver is gone, raising in that task
    stopping = asyncio.ensure_future(manager.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(asyncio.shield(stopping), PLAYWRIGHT_STOP_TIMEOUT_SECONDS)
    except Exception as e:
        log(f"Error stopping Playwright: {first_line(e)}", "WARNING")
    finally:
        kill_driver(manager)

async def run_accounts(accounts, worker, failure_result, options):
    """Run worker(browsers, account, account_num, total_accounts) in parallel, returning results in input order"""
    asyncio.get_running_loop().set_exception_handler(safe_exception_handler)

    # Line numbers and settings only: the account dicts hold passwords
    events.run_started(
        len(accounts),
        options.concurrency,
        options.stagger,
        [account.get('line_num', 0) for account in accounts]
    )
    # On its own thread: the claims never wait for the dashboard or the browser
    dashboard_thread = launcher.launch(log, getattr(options, "no_dashboard", False))

    semaphore = asyncio.Semaphore(options.concurrency)
    pacer_lock = asyncio.Lock()
    next_start = 0.0

    async def wait_for_start_slot():
        nonlocal next_start
        async with pacer_lock:
            now = time.monotonic()
            start = max(now, next_start)
            next_start = start + options.stagger
        await asyncio.sleep(start - now)

    async def run_with_deadline(browsers, account, account_num):
        # Playwright calls on a frozen page or driver never return, and some take
        # no timeout at all. A separate task, not wait_for(): that would wait for
        # the worker to stop, and a worker that won't stop would hang the run.
        email = account['email']
        task = asyncio.ensure_future(worker(browsers, account, account_num, len(accounts)))
        running_workers.add(task)
        task.add_done_callback(running_workers.discard)

        try:
            await asyncio.wait({task}, timeout=ACCOUNT_DEADLINE_SECONDS)
        except asyncio.CancelledError:
            # Interrupted: let the account close before the browser goes
            task.cancel()
            await asyncio.wait({task}, timeout=ACCOUNT_CANCEL_GRACE_SECONDS)

            # Recorded here because the interrupt skips run_account's own record,
            # and a character claimed before it must stay counted
            try:
                events.account_finished(
                    account.get('line_num', 0),
                    email,
                    failure_result(account),
                    timed_out=bool(account.get('timed_out')),
                    interrupted=True
                )
            except Exception:
                pass
            raise

        if task.done():
            return task.result()

        account['timed_out'] = True
        log(f"[{email}] Account deadline of {ACCOUNT_DEADLINE_SECONDS}s passed, stopping this account", "ERROR")
        events.reason(email, "deadline")
        task.cancel()
        await asyncio.wait({task}, timeout=ACCOUNT_CANCEL_GRACE_SECONDS)

        if not task.done():
            log(f"[{email}] Account did not stop, abandoning it", "WARNING")
        elif not task.cancelled():
            # Read it, or asyncio reports it later with no account attached
            task.exception()

        return failure_result(account)

    async def run_account(browsers, account, account_num):
        async with semaphore:
            try:
                # Launch before taking a slot, or accounts queued behind a slow
                # launch would all reach the login page at the same moment
                await browsers.get()
                await wait_for_start_slot()
                events.account_started(account.get('line_num', 0), account['email'])
                result = await run_with_deadline(browsers, account, account_num)
            except Exception as e:
                log(f"Error processing {account['email']}: {first_line(e)}", "ERROR")
                events.reason(account['email'], "error")
                result = failure_result(account)

            events.account_finished(
                account.get('line_num', 0),
                account['email'],
                result,
                timed_out=bool(account.get('timed_out'))
            )
            return result

    # Set only for a run that ended or was interrupted. Any other exit leaves no
    # run_end, so the dashboard shows Crashed, not a Stop nobody pressed
    ending = None
    try:
        # Not "async with": its exit can't be time-limited, and an exception from it
        # would replace the interrupt in flight
        manager = async_playwright()
        p = await manager.__aenter__()

        try:
            browsers = BrowserHolder(
                p,
                pace=wait_for_start_slot,
                block_resources=not getattr(options, "no_block", False)
            )
            try:
                results = await asyncio.gather(*(
                    run_account(browsers, account, account_num)
                    for account_num, account in enumerate(accounts, 1)
                ))
                ending = "finished"
                return results
            finally:
                await close_browser(browsers)
        finally:
            await stop_playwright(manager)
    except (asyncio.CancelledError, KeyboardInterrupt):
        ending = ending or "interrupted"
        raise
    finally:
        if ending:
            events.run_finished(ending)
        # A short run still gets its Dashboard line and browser tab. An interrupt never waits
        if ending == "finished":
            launcher.finish(dashboard_thread)
        events.close()

async def exit_if_stuck(main):
    """Run main, leaving the process directly if a worker task refused to stop"""
    # asyncio.run cancels what is left and waits with no limit, so a task that
    # ignores cancellation would keep the process alive after the summary. By
    # here run_accounts has finished, and its stop_playwright killed the driver.
    try:
        success = await main
    except (asyncio.CancelledError, Exception) as e:
        if not running_workers:
            raise
        if isinstance(e, asyncio.CancelledError):
            log("Interrupted by user", "WARNING")
        else:
            log(f"Fatal error: {first_line(e)}", "ERROR")
        success = False

    if not running_workers:
        return success

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(0 if success else 1)

async def wait_interactive(locator, timeout_ms):
    """Wait until React has hydrated the element, raising on timeout"""
    deadline = time.monotonic() + timeout_ms / 1000

    while True:
        remaining_ms = (deadline - time.monotonic()) * 1000
        if remaining_ms <= 0:
            raise PlaywrightTimeoutError(f"Timed out after {timeout_ms}ms waiting for hydration")

        try:
            # Evaluated afresh each pass so a node replaced by hydration can't hang this
            if await locator.evaluate(IS_HYDRATED_JS, timeout=max(1, min(remaining_ms, 1000))):
                return
        except PlaywrightError:
            # Not in the DOM yet, or the page swapped the node mid-check
            if locator.page.is_closed():
                raise

        await asyncio.sleep(0.1)

async def wait_for_stable_count(locator, stable_ms, timeout_ms):
    """Wait until the locator's match count holds for stable_ms, returning False at timeout_ms"""
    started = time.monotonic()
    stable_since = started
    count = await locator.count()

    while True:
        now = time.monotonic()
        if (now - stable_since) * 1000 >= stable_ms:
            return True
        if (now - started) * 1000 >= timeout_ms:
            return False

        await asyncio.sleep(0.1)

        latest = await locator.count()
        if latest != count:
            count = latest
            stable_since = time.monotonic()

async def click_for_response(page, target, api_path, timeout_ms, request_timeout_ms=None):
    """Click target and wait for its POST to api_path, returning (response or None, click time)

    With request_timeout_ms, gives up early if no POST was sent by then, and adds
    a third item saying whether one was. timeout_ms then runs from the request.
    """
    # A listener, not expect_response(): its timer would run during the click, a
    # click failing after it expired would leave an exception nobody retrieves,
    # and a timed-out click must still raise rather than read as "no response"
    loop = asyncio.get_running_loop()
    request_seen = loop.create_future()
    response_seen = loop.create_future()

    def is_target(request):
        return request.method == "POST" and urlsplit(request.url).path.endswith(api_path)

    def on_request(request):
        if is_target(request) and not request_seen.done():
            request_seen.set_result(request)

    def on_response(response):
        if is_target(response.request):
            on_request(response.request)
            if not response_seen.done():
                response_seen.set_result(response)

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        await target.click()
        clicked_at = time.monotonic()

        if request_timeout_ms is not None:
            try:
                await asyncio.wait_for(request_seen, request_timeout_ms / 1000)
            except asyncio.TimeoutError:
                return None, clicked_at, False

        try:
            response = await asyncio.wait_for(response_seen, timeout_ms / 1000)
        except asyncio.TimeoutError:
            response = None

        return (response, clicked_at) if request_timeout_ms is None else (response, clicked_at, True)
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)

async def dismiss_cookie_banner(page):
    """Best-effort, non-waiting - blocking GTM normally keeps the banner away"""
    for selector in ('#onetrust-accept-btn-handler', 'button:has-text("Accept")'):
        try:
            btn = page.locator(selector).first
            if await btn.is_visible():
                await btn.click(timeout=2000)
                return
        except PlaywrightError:
            continue

def track_sign_in(page):
    """Watch the steps the app runs after the login response, before it signs the player in"""
    state = {"started": False, "stopped": False, "done": asyncio.Event(), "status": None}

    def on_request(request):
        path = urlsplit(request.url).path
        if path.endswith(PROVIDER_TOKEN_API_PATH) or AUTH_CALLBACK_API_PATH in path:
            state["started"] = True

    def on_response(response):
        if AUTH_CALLBACK_API_PATH in urlsplit(response.url).path:
            # The first callback is the sign-in this login started
            if not state["done"].is_set():
                state["status"] = response.status
            state["done"].set()

    def stop():
        # Runs twice on most paths, and a second remove_listener raises KeyError
        if state["stopped"]:
            return
        state["stopped"] = True
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)

    state["stop"] = stop
    page.on("request", on_request)
    page.on("response", on_response)
    return state

async def wait_for_sign_in(sign_in, email, start_timeout=0):
    """Let a started sign-in finish - leaving early strands the app without a session"""
    # The header still shows the player without one, but Claim Now! then logs
    # out and redirects to /login instead of claiming
    try:
        deadline = time.monotonic() + start_timeout
        while not sign_in["started"] and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        if sign_in["started"]:
            await asyncio.wait_for(sign_in["done"].wait(), SIGN_IN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log(f"[{email}] Sign-in did not finish in {SIGN_IN_TIMEOUT_SECONDS}s, continuing", "WARNING")
    finally:
        sign_in["stop"]()

async def wait_for_login_outcome(page, sign_in, deadline):
    """Wait for the character dialog or a finished sign-in, returning dialog, signed_in or timeout"""
    # Polled, not raced as tasks: a cancelled wait_for leaves an exception nobody
    # retrieves. The dialog is checked first on every pass, so it always wins.
    dialog = page.locator('text=Select the Character').locator('visible=true').first
    while True:
        remaining_ms = (deadline - time.monotonic()) * 1000
        try:
            await dialog.wait_for(state="visible", timeout=max(1, min(remaining_ms, LOGIN_OUTCOME_POLL_MS)))
            return "dialog"
        except PlaywrightTimeoutError:
            if sign_in["done"].is_set():
                return "signed_in"
            if time.monotonic() >= deadline:
                return "timeout"

async def login(page, email, password, pace=None):
    """Log in and wait for the character dialog or the sign-in, returning False if login was refused"""
    events.stage(email, "login")
    sign_in = track_sign_in(page)
    try:
        # A retry redoes the page and the form: their state after a 5xx is unknown,
        # and a re-click that sent nothing would wait 30s and carry on logged out
        for attempt in (1, 2):
            log(f"[{email}] Loading login page...")
            response = await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            if not response or not response.ok:
                log(f"[{email}] Failed to load page: {response.status if response else 'No response'}", "ERROR")
                events.reason(email, "page_load_failed", response.status if response else None)
                return False

            # Inputs reach the DOM long before hydration, which wipes anything typed early
            email_input = page.locator('input').first
            login_button = page.locator('button:has-text("Login")')
            await wait_interactive(email_input, HYDRATION_TIMEOUT_MS)
            await wait_interactive(login_button, HYDRATION_TIMEOUT_MS)

            # Fill login form
            log(f"[{email}] Filling credentials...")
            await email_input.fill(email)
            await page.locator('input').nth(1).fill(password)

            await dismiss_cookie_banner(page)

            # Login
            log(f"[{email}] Logging in...")
            response, clicked_at = await click_for_response(page, login_button, LOGIN_API_PATH, LOGIN_RESPONSE_TIMEOUT_MS)
            settled_at = time.monotonic()

            if response is None:
                log(f"[{email}] Login response not observed, continuing", "WARNING")
                break

            if response.ok:
                events.stage(email, "signin")
                break

            if attempt == 2 or not 500 <= response.status <= 599:
                log(f"[{email}] Login rejected: HTTP {response.status}", "ERROR")
                events.reason(email, "login_rejected", response.status)
                return False

            log(f"[{email}] Login failed: HTTP {response.status}, retrying in {LOGIN_RETRY_BACKOFF_SECONDS}s", "WARNING")
            await asyncio.sleep(LOGIN_RETRY_BACKOFF_SECONDS)

            # Takes a normal slot, so a retry can't break the global login spacing
            if pace is not None:
                await pace()

        deadline = settled_at + LOGIN_OUTCOME_TIMEOUT_SECONDS
        outcome = await wait_for_login_outcome(page, sign_in, deadline)
        elapsed = time.monotonic() - clicked_at

        if outcome == "dialog":
            # Sign-in only follows the character choice here
            sign_in["stop"]()
            log(f"[{email}] Character dialog appeared {elapsed:.1f}s after the Login click")
            return True

        if outcome == "signed_in":
            log(f"[{email}] Signed in with no character dialog {elapsed:.1f}s after the Login click")
        elif not sign_in["started"]:
            log(
                f"[{email}] No character dialog and no sign-in {LOGIN_OUTCOME_TIMEOUT_SECONDS:g}s after the login response, "
                "continuing as a single character account",
                "WARNING"
            )

        await wait_for_sign_in(sign_in, email)

        status = sign_in["status"]
        if status is not None and not 200 <= status <= 299:
            log(f"[{email}] Sign-in callback returned HTTP {status}", "WARNING")

        # Unconfirmed on the real site that a dialog never follows the sign-in, and
        # only this page can show it: the single character path leaves for the vault
        if outcome == "signed_in":
            try:
                await page.locator('text=Select the Character').locator('visible=true').first.wait_for(
                    state="visible",
                    timeout=max(1, LOGIN_DIALOG_GRACE_MS)
                )
                log(f"[{email}] Character dialog appeared after sign-in, treating as multi-character", "WARNING")
            except PlaywrightTimeoutError:
                pass

        return True
    finally:
        sign_in["stop"]()

def claim_button(page):
    """The visible Claim Now! button - being there proves nothing about the session"""
    return page.locator('button:has-text("Claim Now!")').locator('visible=true').first

def cooldown_message(page):
    """Visible proof the gift is claimed"""
    # Visible elements only: these strings sit in the __NEXT_DATA__ i18n JSON on
    # every page, so raw HTML always matches. "remaining!" keeps its exclamation
    # mark because a claimable card carries a "1 Remaining" badge.
    return (
        page.locator('.gift-expire-message')
        .or_(page.get_by_text('Available again at'))
        .or_(page.get_by_text('remaining!'))
        .or_(page.get_by_text('You have claimed the current offer'))
        .locator('visible=true')
        .first
    )

async def wait_for_session(session_requests, email):
    """Let the app's session load finish, because a click before that signs the player out"""
    # The app treats "session still loading" as logged out: the click then skips
    # the claim, logs out and redirects to /login, with no error anywhere
    await asyncio.sleep(SESSION_SETTLE_SECONDS)

    # The fetch can start after the button hydrates. Once one has, this costs nothing
    deadline = time.monotonic() + SESSION_REQUEST_TIMEOUT_SECONDS
    while not session_requests and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    if not session_requests:
        log(f"[{email}] No session request seen, continuing", "WARNING")
        return

    try:
        response = await asyncio.wait_for(session_requests[-1].response(), SESSION_TIMEOUT_SECONDS)
        if response:
            body = await asyncio.wait_for(response.text(), SESSION_TIMEOUT_SECONDS)
            if body.strip() in ("", "{}", "null"):
                log(f"[{email}] App session is empty, so the claim will not work", "WARNING")
        else:
            log(f"[{email}] Session load not confirmed: no response", "WARNING")
    except (asyncio.TimeoutError, PlaywrightError) as e:
        log(f"[{email}] Session load not confirmed: {first_line(e) if str(e) else type(e).__name__}", "WARNING")

    await asyncio.sleep(SESSION_SETTLE_SECONDS)

async def vault_state(page, email):
    """Open the vault and report its state as claim, cooldown or unknown"""
    events.stage(email, "vault")
    session_requests = []

    def on_request(request):
        if urlsplit(request.url).path.endswith(SESSION_API_PATH):
            session_requests.append(request)

    def on_frame_navigated(frame):
        # Only the document the claim runs on counts: a finished request from an
        # earlier page would end the session wait before this page's own fetch.
        # The first commit always clears, because a character's vault load comes
        # from the vault itself. After that the path must change, because the app
        # rewriting its own URL also lands here and leaves the session alone.
        nonlocal document_path
        path = urlsplit(frame.url).path
        if frame == page.main_frame and path != document_path:
            document_path = path
            session_requests.clear()

    page.on("request", on_request)
    page.on("framenavigated", on_frame_navigated)
    try:
        for attempt in (1, 2):
            document_path = None
            try:
                await page.goto(VAULT_URL, wait_until="domcontentloaded", timeout=60000)
                break
            except PlaywrightError as e:
                if attempt == 2 or "net::ERR_ABORTED" not in str(e):
                    raise

            # Probably racing the app's own navigation after login
            log(f"[{email}] Vault navigation aborted, retrying in {VAULT_RETRY_DELAY_SECONDS}s", "WARNING")
            await asyncio.sleep(VAULT_RETRY_DELAY_SECONDS)

        button = claim_button(page)
        cooldown = cooldown_message(page)

        try:
            await button.or_(cooldown).first.wait_for(state="visible", timeout=VAULT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            events.reason(email, "no_claim_button")
            return "unknown"

        # Re-check: whichever appeared first, a claim button wins
        if await button.is_visible():
            try:
                await wait_interactive(button, VAULT_TIMEOUT_MS)
                await wait_for_session(session_requests, email)
                return "claim"
            except PlaywrightTimeoutError:
                pass

        if await cooldown.is_visible():
            return "cooldown"

        events.reason(email, "no_claim_button")
        return "unknown"
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("framenavigated", on_frame_navigated)

async def capture_screenshot(page):
    """Take a screenshot over CDP, returning the PNG bytes"""
    # page.screenshot() waits for document.fonts.ready, which stays pending until
    # the load event, and the flow no longer waits for load
    session = await page.context.new_cdp_session(page)
    result = await session.send("Page.captureScreenshot", {"format": "png"})
    await session.detach()
    return base64.b64decode(result['data'])

async def save_screenshot(page, path, email):
    """Save a screenshot - it is only evidence, so a failure must never cost the claim"""
    try:
        png = await asyncio.wait_for(capture_screenshot(page), CLAIM_SCREENSHOT_TIMEOUT_SECONDS)
        with open(path, 'wb') as f:
            f.write(png)
    except Exception as e:
        log(f"[{email}] Screenshot not saved: {first_line(e)}", "WARNING")

async def claim_accepted(page, email, password):
    """Click Claim and judge the response by the app's own rule, logging why if it failed"""
    events.stage(email, "claim")
    secrets = (password,)

    try:
        response, _, request_sent = await click_for_response(
            page,
            claim_button(page),
            CLAIM_API_PATH,
            CLAIM_RESPONSE_TIMEOUT_MS,
            request_timeout_ms=CLAIM_REQUEST_TIMEOUT_MS
        )
    except PlaywrightError as e:
        log(f"[{email}] Claim failed: {sanitize_detail(first_line(e), secrets)}", "ERROR")
        events.reason(email, "claim_no_request")
        return False

    if response is None:
        # Never success: this text follows an ok response and a second fetch, so
        # seeing it here means the listener or the endpoint is wrong. A false
        # FAILED corrects itself next run; a false CLAIMED reports jewels that
        # never existed.
        reason = (f"claim request sent, no response in {CLAIM_RESPONSE_TIMEOUT_MS / 1000:g}s"
                  if request_sent else "click sent no claim request")
        try:
            # Bounded: a frozen page never answers, and it must not hold up the run
            if await asyncio.wait_for(cooldown_message(page).is_visible(), CLAIM_BODY_TIMEOUT_SECONDS):
                reason += " (cooldown text is visible)"
        except (asyncio.TimeoutError, PlaywrightError):
            pass
        log(f"[{email}] Claim failed: {reason}", "ERROR")
        events.reason(email, "claim_no_response" if request_sent else "claim_no_request")
        return False

    try:
        body = json.loads(await asyncio.wait_for(response.text(), CLAIM_BODY_TIMEOUT_SECONDS))
        readable = True
    except Exception:
        body = None
        readable = False

    fields = body if isinstance(body, dict) else {}

    if response.ok and not readable:
        log(f"[{email}] Claim failed: HTTP {response.status}, unreadable response", "ERROR")
        events.reason(email, "claim_unreadable", response.status)
        return False

    if response.ok and not js_truthy(fields.get('error')) and not js_truthy(fields.get('errorCode')):
        return True

    details = " ".join(
        f"{name}={sanitize_detail(fields.get(name), secrets)}"
        for name in ('error', 'errorCode', 'errorMessage')
    )
    log(f"[{email}] Claim failed: HTTP {response.status} {details}", "ERROR")
    events.reason(email, "claim_rejected", response.status)
    return False

async def submit_claim(page, email, password, failure_screenshot):
    """Claim, returning True only if the server accepted it, and leave evidence if not"""
    # The app reports failure only through console.error and a toast
    page_errors = []
    network = []

    def on_console(message):
        # A failed load is already in the network list, and blocking makes many
        # of them - left in, they crowd out the line that explains the failure
        if (message.type == "error" and len(page_errors) < CLAIM_CONSOLE_LIMIT
                and not message.text.startswith("Failed to load resource")):
            page_errors.append(message.text)

    def on_page_error(error):
        if len(page_errors) < CLAIM_CONSOLE_LIMIT:
            page_errors.append(str(error))

    def on_response(response):
        path = urlsplit(response.url).path
        # Analytics fires constantly, and would crowd out the calls that matter
        if ("/api/" in path and ANALYTICS_TUNNEL_PATH not in path
                and len(network) < CLAIM_NETWORK_LIMIT):
            network.append((response.request.method, path, f"HTTP {response.status}"))

    def on_request_failed(request):
        url = urlsplit(request.url)
        if ANALYTICS_TUNNEL_PATH not in url.path and len(network) < CLAIM_NETWORK_LIMIT:
            network.append((request.method, f"{url.hostname}{url.path}", request.failure))

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    try:
        accepted = await claim_accepted(page, email, password)

        # The app logs only after it has parsed the response itself
        if not accepted:
            await asyncio.sleep(CLAIM_CONSOLE_SETTLE_SECONDS)
    finally:
        page.remove_listener("console", on_console)
        page.remove_listener("pageerror", on_page_error)
        page.remove_listener("response", on_response)
        page.remove_listener("requestfailed", on_request_failed)

    if not accepted:
        for text in page_errors:
            log(f"[{email}] Console: {sanitize_detail(text, (password,))}", "WARNING")
        if not network:
            log(f"[{email}] Network: no API calls or failed requests during the click", "WARNING")
        for method, target, outcome in network:
            log(f"[{email}] Network: {method} {sanitize_path(target, (password,))} -> "
                f"{sanitize_detail(outcome, (password,))}", "WARNING")
        await save_screenshot(page, failure_screenshot, email)

    return accepted

async def select_character(page, button, email):
    """Click a character card, let the selection land, and return whether this character is now active"""
    events.stage(email, "signin")

    # The current character may not POST at all, so don't wait long for it
    classes = await button.get_attribute("class") or ""
    already_selected = "selected-profile" in classes.split()
    timeout_ms = SELECTED_PROFILE_TIMEOUT_MS if already_selected else SELECT_PROFILE_TIMEOUT_MS

    sign_in = track_sign_in(page)
    try:
        # Navigating too soon could cancel the request and claim for the wrong character
        response, _ = await click_for_response(page, button, SELECT_PROFILE_API_PATH, timeout_ms)
        await asyncio.sleep(SESSION_SETTLE_SECONDS)

        # Sign-in follows a successful selection, sometimes after a pause. With no
        # selection there is nothing to wait for, and the warning below is due
        selected = response is not None and response.ok
        await wait_for_sign_in(sign_in, email, start_timeout=SIGN_IN_START_TIMEOUT_SECONDS if selected else 0)
    finally:
        sign_in["stop"]()

    if response is None:
        log(f"[{email}] select-profile not observed, continuing", "WARNING")
    elif not response.ok:
        log(f"[{email}] select-profile failed: HTTP {response.status}", "WARNING")

    # A claim after a failed switch would land on the previous character. With no
    # response seen, a finished sign-in still shows the page switched
    switched = already_selected or selected or (response is None and sign_in["done"].is_set())
    if not switched:
        events.reason(email, "switch_failed")
    return switched
