#!/usr/bin/env python3
"""
Game of War - Connection status for the dashboard
Checks the internet and the game site on a daemon thread, only while someone is
looking or a run is live. This is information only: nothing here can reach a
run, and a request for the status never waits on a check.
"""

import os
import ssl
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

DEFAULT_INTERNET_URL = "https://www.gstatic.com/generate_204"
DEFAULT_BASE_URL = "https://www.gameofwar-fireage.com/en-us"
USER_AGENT = "GoW-Dashboard/1"

INTERNET_INTERVAL_SECONDS = 30
SITE_INTERVALS_PER_INTERNET = 4
INTERNET_TIMEOUT_SECONDS = 5
SITE_TIMEOUT_SECONDS = 10
INTERNET_RECHECK_SECONDS = 5
SITE_RECHECK_SECONDS = 15
# urllib's timeout does not cover DNS, so a check can outlive it
HARD_DEADLINE_EXTRA_SECONDS = 2
MAX_ABANDONED_CHECKS = 3
STALE_INTERVALS = 3
VIEWER_WINDOW_SECONDS = 15
IDLE_POLL_SECONDS = 5
FAILURES_TO_CHANGE = 2

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer in itself: a captive portal must not read as the internet"""

    def redirect_request(self, *args, **kwargs):
        return None

def valid_url(url):
    """True for an http or https URL with a hostname"""
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)

def without_credentials(url):
    """The URL with any user and password removed, so they are never sent"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, parts.query, ""))

def load_targets():
    """(internet URL or None, site URL). Raises ValueError, never quoting the value"""
    internet = os.environ.get("GOW_INTERNET_CHECK_URL")
    base = os.environ.get("GOW_BASE_URL")

    if internet is None and base is None:
        internet = DEFAULT_INTERNET_URL

    # Otherwise, as with the event file, a mock run stays on loopback: with a site
    # override and no internet target, only the site is checked
    if internet is not None and not valid_url(internet):
        raise ValueError("GOW_INTERNET_CHECK_URL must be an http or https URL")

    site = f"{base if base is not None else DEFAULT_BASE_URL}/login"
    if not valid_url(site):
        raise ValueError("GOW_BASE_URL must be an http or https URL")

    return (without_credentials(internet) if internet is not None else None), without_credentials(site)

def load_interval():
    """The internet check interval: GOW_NET_INTERVAL_SECONDS is for tests"""
    try:
        value = float(os.environ.get("GOW_NET_INTERVAL_SECONDS", ""))
    except ValueError:
        return INTERNET_INTERVAL_SECONDS
    return value if value > 0 else INTERNET_INTERVAL_SECONDS

def failure_kind(error):
    """One fixed word for why a check failed. Exception text can hold the URL"""
    reason = getattr(error, "reason", error)
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    return "error"

def probe(url, method, timeout):
    """Make one request, returning (status or None, failure or None). The body is never read"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})

        with opener.open(request, timeout=timeout) as response:
            return response.status, None
    except urllib.error.HTTPError as e:
        status = e.code
        e.close()
        return status, None
    except Exception as e:
        return None, failure_kind(e)

class CheckResult:
    """The shown result of one target, after debounce"""

    def __init__(self):
        self.passed = None
        self.latest_passed = None
        self.ms = None
        self.status = None
        self.failure = None
        self.at = None
        self.failures = 0

class NetworkMonitor:
    """Runs the checks on its own thread. viewed() and status() only take a short lock"""

    def __init__(self, is_run_live):
        self.is_run_live = is_run_live
        self.internet_url, self.site_url = load_targets()
        self.internet_interval = load_interval()
        self.site_interval = self.internet_interval * SITE_INTERVALS_PER_INTERNET
        # Capped so a short interval in a test isn't outrun by its own recheck
        self.internet_recheck = min(INTERNET_RECHECK_SECONDS, self.internet_interval)
        self.site_recheck = min(SITE_RECHECK_SECONDS, self.site_interval)

        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.internet = CheckResult()
        self.site = CheckResult()
        self.last_view = None
        self.idle = True
        self.check_now = False
        self.abandoned = 0
        self.next_internet = 0
        self.next_site = 0
        self.last_site_attempt = None
        self.thread = threading.Thread(target=self.run, name="gow-network", daemon=True)

    def start(self):
        self.thread.start()

    def viewed(self):
        """Note that someone is looking, waking the checks if they had gone idle"""
        with self.lock:
            self.last_view = time.monotonic()
            if self.idle:
                self.idle = False
                self.check_now = True
                self.wake.set()

    def is_stale(self, result, interval, timeout, now):
        if result.at is None:
            return True

        # A failure holds until its replacement could have arrived, or a hung
        # check would flip it to checking while the next one is still running
        limit = interval * STALE_INTERVALS
        if not result.passed:
            limit += timeout + HARD_DEADLINE_EXTRA_SECONDS
        return now - result.at > limit

    def internet_stale(self, now):
        return self.is_stale(self.internet, self.internet_interval, INTERNET_TIMEOUT_SECONDS, now)

    def site_stale(self, now):
        return self.is_stale(self.site, self.site_interval, SITE_TIMEOUT_SECONDS, now)

    def status(self):
        """The current state, with fixed fields only: no URL, hostname or exception text"""
        with self.lock:
            now = time.monotonic()
            internet, site = self.internet, self.site
            checked = [result.at for result in (internet, site) if result.at is not None]
            age = round(now - max(checked)) if checked else None
            unknown = {"internet_ms": None, "site_ms": None, "site_status": None, "failure": None}

            if self.internet_url is not None:
                if self.internet_stale(now):
                    return {"state": "checking", "checked_seconds_ago": age, **unknown}
                if not internet.passed:
                    return {"state": "no_internet", "checked_seconds_ago": age, **unknown, "failure": internet.failure}

            if self.site_stale(now):
                return {"state": "checking", "checked_seconds_ago": age, **unknown}

            return {
                "state": "ok" if site.passed else "site_unreachable",
                "checked_seconds_ago": age,
                "internet_ms": internet.ms if self.internet_url is not None else None,
                "site_ms": site.ms if site.passed else None,
                "site_status": site.status,
                "failure": None if site.passed else site.failure,
            }

    def record(self, result, stale, passed, ms, status, failure):
        """Store a check's outcome, returning True if a recheck should follow soon"""
        with self.lock:
            now = time.monotonic()
            running_ok = result.passed and not stale(now)
            result.latest_passed = passed
            result.at = now

            if passed:
                result.failures = 0
            else:
                result.failures += 1

            # One slow answer in a busy run must not raise a banner, but a first
            # result, or one after an idle gap, shows at once
            if running_ok and not passed and result.failures < FAILURES_TO_CHANGE:
                return True

            result.passed = passed
            result.ms = ms if passed else None
            result.status = status
            result.failure = None if passed else failure
            return False

    def bounded_probe(self, url, method, timeout):
        """Probe on a thread of its own, returning (status, failure, ms), or None if none can start"""
        with self.lock:
            if self.abandoned >= MAX_ABANDONED_CHECKS:
                return None

        outcome = {}
        finished = threading.Event()
        lock = self.lock
        state = {"abandoned": False}

        def check():
            started = time.monotonic()
            status, failure = probe(url, method, timeout)
            with lock:
                outcome.update(status=status, failure=failure, ms=round((time.monotonic() - started) * 1000))
                if state["abandoned"]:
                    self.abandoned -= 1
            finished.set()

        threading.Thread(target=check, name="gow-network-check", daemon=True).start()

        if not finished.wait(timeout + HARD_DEADLINE_EXTRA_SECONDS):
            with lock:
                if not outcome:
                    # Any late result is dropped: this check has already failed
                    state["abandoned"] = True
                    self.abandoned += 1
                    return None, "timeout", None

        return outcome["status"], outcome["failure"], outcome["ms"]

    def check_internet(self):
        checked = self.bounded_probe(self.internet_url, "GET", INTERNET_TIMEOUT_SECONDS)
        now = time.monotonic()
        if checked is None:
            self.next_internet = now + self.internet_interval
            return

        was_down = self.internet.latest_passed is False
        status, failure, ms = checked
        passed = status == 204
        if status is not None and not passed:
            failure = "http"

        recheck = self.record(self.internet, self.internet_stale, passed, ms, status, failure)
        self.next_internet = now + (self.internet_recheck if recheck else self.internet_interval)

        # Back up: look at the site at once, but at most once per internet interval
        if was_down and passed:
            earliest = now if self.last_site_attempt is None else self.last_site_attempt + self.internet_interval
            self.next_site = min(self.next_site, max(now, earliest))

    def check_site(self):
        checked = self.bounded_probe(self.site_url, "HEAD", SITE_TIMEOUT_SECONDS)
        now = time.monotonic()
        self.last_site_attempt = now
        if checked is None:
            self.next_site = now + self.site_interval
            return

        status, failure, ms = checked
        passed = status is not None and status < 500
        if status is not None and not passed:
            failure = "http"

        recheck = self.record(self.site, self.site_stale, passed, ms, status, failure)
        self.next_site = now + (self.site_recheck if recheck else self.site_interval)

    def wanted(self):
        """True while someone looked recently or a run is live"""
        with self.lock:
            viewed = self.last_view is not None and time.monotonic() - self.last_view < VIEWER_WINDOW_SECONDS
        if viewed:
            return True

        try:
            return bool(self.is_run_live())
        except Exception:
            return False

    def internet_is_up(self):
        return self.internet_url is None or bool(self.internet.latest_passed)

    def run(self):
        while True:
            self.wake.clear()

            if not self.wanted():
                with self.lock:
                    self.idle = True
                self.wake.wait(IDLE_POLL_SECONDS)
                continue

            with self.lock:
                self.idle = False
                if self.check_now:
                    self.check_now = False
                    self.next_internet = 0

            if self.internet_url is not None and time.monotonic() >= self.next_internet:
                self.check_internet()

            # With the internet down, a failure would say nothing about the site
            if self.internet_is_up() and time.monotonic() >= self.next_site:
                self.check_site()

            pending = [self.next_internet] if self.internet_url is not None else []
            if self.internet_is_up():
                pending.append(self.next_site)

            delay = min(pending) - time.monotonic()
            self.wake.wait(min(max(delay, 0.05), IDLE_POLL_SECONDS))
