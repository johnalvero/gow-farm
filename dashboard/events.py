#!/usr/bin/env python3
"""
Game of War - Run event recorder
Writes what a claim run did to a local file, so the dashboard can show it.
Nothing here may fail, slow or change a claim: every public function only puts
an event on a queue, and a daemon thread does all of the file work.
"""

import fcntl
import json
import os
import queue
import re
import stat
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(BASE_DIR, "data", "events.jsonl")

SCHEMA_VERSION = 1
MAX_QUEUE = 10000
MAX_EVENT_BYTES = 20_000_000
MAX_LINES = 5000
MAX_LINE_NUMBER = 9_999_999
CLOSE_TIMEOUT_SECONDS = 1
LOCK_ATTEMPTS = 5
LOCK_RETRY_SECONDS = 0.05

STAGES = ("login", "signin", "vault", "claim")
REASONS = (
    "page_load_failed", "login_rejected", "no_claim_button", "claim_no_request", "claim_no_response",
    "claim_rejected", "claim_unreadable", "switch_failed", "deadline", "error",
)
RUN_STATUSES = ("finished", "interrupted")
SOURCES = ("terminal", "dashboard")
SCRIPTS = ("claim_all_characters.py", "claim_all.py", "claim_batch.py")

RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]{1,10}")
# The masked shape: kept for values that aren't plain emails, and for older events
LABEL_RE = re.compile(r"[A-Za-z0-9?]\*\*\*(?:@[A-Za-z0-9?]\*\*\*)?")
EMAIL_LABEL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
MAX_EMAIL_LABEL = 254

def is_count(value, high):
    return type(value) is int and 0 <= value <= high

def is_seconds(value, high):
    # Comparisons alone: NaN and infinity both fail them, and math.isfinite would
    # raise on a huge integer from a damaged file
    return type(value) in (int, float) and 0 <= value <= high

def is_line(value):
    return is_count(value, MAX_LINE_NUMBER)

def is_email_label(value):
    return len(value) <= MAX_EMAIL_LABEL and EMAIL_LABEL_RE.fullmatch(value) is not None

def is_label(value):
    return isinstance(value, str) and (is_email_label(value) or LABEL_RE.fullmatch(value) is not None)

def is_lines(value):
    return type(value) is list and len(value) <= MAX_LINES and all(is_line(line) for line in value)

def is_flag(value):
    return type(value) is bool

def one_of(allowed):
    return lambda value: isinstance(value, str) and value in allowed

def is_http_status(value):
    return type(value) is int and 100 <= value <= 599

def is_timestamp(value):
    return is_seconds(value, 4_102_444_800)

# Field checkers per event, and fields that may be absent. Every string is an
# enum, an email-shaped or masked label, or the run id: there is no field for
# free text, so no log line, exception or page text can reach the file or the page.
COMMON_FIELDS = {
    "v": lambda value: type(value) is int and value == SCHEMA_VERSION,
    "run": lambda value: isinstance(value, str) and RUN_ID_RE.fullmatch(value) is not None,
    "ts": is_timestamp,
    "event": lambda value: value in EVENT_FIELDS,
}
EVENT_FIELDS = {
    "run_start": {
        "source": one_of(SOURCES),
        "script": one_of(SCRIPTS),
        "concurrency": lambda value: is_count(value, 100000),
        "stagger": lambda value: is_seconds(value, 1e9),
        "total": lambda value: is_count(value, MAX_LINE_NUMBER),
        "lines": is_lines,
    },
    "account_start": {"line": is_line, "label": is_label},
    "stage": {"line": is_line, "stage": one_of(STAGES)},
    "reason": {"line": is_line, "reason": one_of(REASONS), "status": is_http_status},
    "account_end": {
        "line": is_line,
        "label": is_label,
        "characters": lambda value: is_count(value, 100000),
        "claimed": lambda value: is_count(value, 100000),
        "already": lambda value: is_count(value, 100000),
        "success": is_flag,
        "timed_out": is_flag,
        "interrupted": is_flag,
        "seconds": lambda value: is_seconds(value, 1e7),
    },
    "run_end": {"status": one_of(RUN_STATUSES), "dropped": lambda value: is_count(value, 10**12)},
}
OPTIONAL_FIELDS = {"reason": {"status"}, "run_end": {"dropped"}}

def validate_event(event):
    """True only if the event has exactly the allowed fields, each passing its check"""
    if type(event) is not dict or not isinstance(event.get("event"), str):
        return False

    fields = EVENT_FIELDS.get(event["event"])
    if fields is None:
        return False

    allowed = {**COMMON_FIELDS, **fields}
    required = set(allowed) - OPTIONAL_FIELDS.get(event["event"], set())
    if not required <= set(event) <= set(allowed):
        return False

    return all(allowed[name](value) for name, value in event.items())

def mask_account(email):
    """Reduce an email to the first character of each part, such as j***@g***"""
    def first(part):
        character = part[:1]
        return character if character.isascii() and character.isalnum() else "?"

    local, separator, domain = str(email).rpartition("@")
    if not separator:
        return f"{first(domain)}***"
    return f"{first(local)}***@{first(domain)}***"

def account_label(email):
    """The account's label: the address itself if it is plainly an email, otherwise masked"""
    # Masked otherwise because the field isn't always an email: a value with no
    # @ may be a password, from a line with its two parts swapped
    text = str(email)
    return text if is_email_label(text) else mask_account(text)

def events_path():
    """The event file, or None when recording is off"""
    configured = os.environ.get("GOW_EVENTS_FILE")
    if configured is not None:
        return os.path.abspath(configured) if configured else None

    # A mock run must not write into real history
    if os.environ.get("GOW_BASE_URL"):
        return None

    return DEFAULT_PATH

def locks_dir(path):
    return f"{path}.locks"

def lock_path(path, run_id):
    return os.path.join(locks_dir(path), f"{run_id}.lock")

def lock_held(path):
    """True if a live process holds this lock file. A process's lock goes when it does"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)

def private_dir(path):
    """Create a directory only this user can read, refusing a symlink in its place"""
    if os.path.islink(path):
        raise OSError(f"{path} is a symlink")
    os.makedirs(path, mode=0o700, exist_ok=True)

def write_line(fd, data):
    """Write one event line - the only place that touches the disk for an event"""
    while data:
        data = data[os.write(fd, data):]

class Recorder:
    """One run's events, written by a daemon thread so the event loop never waits on the disk"""

    def __init__(self, path, run_id):
        self.path = path
        self.run_id = run_id
        self.queue = queue.Queue(maxsize=MAX_QUEUE)
        self.accounts = {}
        self.dropped = 0
        self.stopped = False
        self.lock_fd = None
        self.thread = threading.Thread(target=self.write_loop, name="gow-events", daemon=True)

    def put(self, name, **fields):
        if self.stopped:
            return

        event = {"v": SCHEMA_VERSION, "run": self.run_id, "ts": round(time.time(), 3), "event": name, **fields}
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def take_lock(self):
        private_dir(locks_dir(self.path))
        target = lock_path(self.path, self.run_id)
        self.lock_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)

        # Held for the life of the process: this is what proves the run is alive.
        # A short retry, because another run may be probing the file for a moment
        for attempt in range(LOCK_ATTEMPTS):
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if attempt == LOCK_ATTEMPTS - 1:
                    raise
                time.sleep(LOCK_RETRY_SECONDS)

    def another_run_is_live(self):
        own = os.path.basename(lock_path(self.path, self.run_id))
        directory = locks_dir(self.path)
        return any(
            lock_held(os.path.join(directory, name))
            for name in os.listdir(directory)
            if name != own
        )

    def rotate(self):
        try:
            size = os.stat(self.path).st_size
        except FileNotFoundError:
            return

        if size > MAX_EVENT_BYTES and not self.another_run_is_live():
            os.replace(self.path, f"{self.path}.1")

    def open_file(self):
        # Checked first, so a FIFO or a directory leaves nothing beside it
        try:
            if not stat.S_ISREG(os.lstat(self.path).st_mode):
                raise OSError(f"{self.path} is not a regular file")
        except FileNotFoundError:
            pass

        private_dir(os.path.dirname(self.path))
        self.take_lock()
        self.rotate()

        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(f"{self.path} is not a regular file")

        # A short write may have left a partial line, which would swallow this
        # run's run_start and hide the run from the dashboard
        if not self.ends_with_newline():
            write_line(fd, b"\n")
        return fd

    def ends_with_newline(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            size = os.fstat(fd).st_size
            return size == 0 or os.pread(fd, 1, size - 1) == b"\n"
        finally:
            os.close(fd)

    def write_loop(self):
        fd = None
        try:
            while True:
                event = self.queue.get()
                if event is None:
                    return

                if event["event"] == "run_end":
                    event["dropped"] = self.dropped

                if not validate_event(event):
                    self.dropped += 1
                    if fd is None:
                        # No valid run_start means no run for later events to join
                        self.stopped = True
                        return
                    continue

                if fd is None:
                    fd = self.open_file()

                write_line(fd, encode_event(event))

                if event["event"] == "run_end":
                    # The run is on record as ended, so the lock file has no more use
                    os.unlink(lock_path(self.path, self.run_id))
        except Exception:
            # Off for the rest of the run, silently: a claim must never notice
            self.stopped = True

def encode_event(event):
    return json.dumps(event, separators=(",", ":"), allow_nan=False).encode() + b"\n"

recorder = None

def run_started(total, concurrency, stagger, lines):
    """Start recording a run. Passing line numbers only: account details stay with the caller"""
    global recorder
    try:
        recorder = None
        path = events_path()
        if path is None:
            return

        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
        source = os.environ.get("GOW_RUN_SOURCE")
        current = Recorder(path, run_id)
        current.put(
            "run_start",
            source=source if source in SOURCES else "terminal",
            script=os.path.basename(sys.argv[0]),
            concurrency=concurrency,
            stagger=stagger,
            total=total,
            lines=list(lines[:MAX_LINES])
        )
        current.thread.start()
        recorder = current
    except Exception:
        recorder = None

def account_started(line, email):
    """Record an account starting, identified by line and shown by its label"""
    try:
        if recorder is not None:
            label = account_label(email)
            recorder.accounts[email] = {"line": line, "label": label, "started": time.monotonic()}
            recorder.put("account_start", line=line, label=label)
    except Exception:
        pass

def stage(email, name):
    """Record the step an account has reached"""
    try:
        if recorder is not None and email in recorder.accounts:
            recorder.put("stage", line=recorder.accounts[email]["line"], stage=name)
    except Exception:
        pass

def reason(email, name, status=None):
    """Record why an account failed, as a fixed reason and an optional HTTP status"""
    try:
        if recorder is not None and email in recorder.accounts:
            fields = {} if status is None else {"status": status}
            recorder.put("reason", line=recorder.accounts[email]["line"], reason=name, **fields)
    except Exception:
        pass

def result_counts(result):
    """Normalize a worker result to (characters, claimed, already)"""
    # Two shapes: a dict from claim_all_characters.py, and an outcome string from
    # the other two scripts. The outcome strings are claim_common's constants.
    if isinstance(result, dict):
        return result.get("total", 0), result.get("claimed", 0), result.get("already", 0)
    if result == "claimed":
        return 1, 1, 0
    if result == "already_claimed":
        return 1, 0, 1
    return 0, 0, 0

def account_finished(line, email, result, timed_out=False, interrupted=False):
    """Record an account's result, by the same success rule as the scripts' summaries"""
    try:
        if recorder is None:
            return

        known = recorder.accounts.get(email)
        seconds = round(time.monotonic() - known["started"], 1) if known else 0
        characters, claimed, already = result_counts(result)
        recorder.put(
            "account_end",
            line=line,
            label=account_label(email),
            characters=characters,
            claimed=claimed,
            already=already,
            success=claimed + already > 0 and not timed_out,
            timed_out=timed_out,
            interrupted=interrupted,
            seconds=seconds
        )
    except Exception:
        pass

def run_finished(status):
    """Record the end of the run"""
    try:
        if recorder is not None:
            recorder.put("run_end", status=status)
    except Exception:
        pass

def close():
    """Let the writer finish, waiting a bounded time so a stalled disk can't hold the exit"""
    try:
        if recorder is not None:
            recorder.queue.put_nowait(None)
            recorder.thread.join(CLOSE_TIMEOUT_SECONDS)
    except Exception:
        pass
