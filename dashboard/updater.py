#!/usr/bin/env python3
"""
Game of War - Updates from GitHub for the dashboard
Checks the project's GitHub remote for new commits on a daemon thread, and
fast-forwards the checkout when asked. Git's own output is only ever parsed for
hashes and counts: it is never logged, returned or shown, since it can hold the
remote's URL. A request for the status never waits on git.
"""

import os
import signal
import sys
import threading
import time
import warnings
from datetime import datetime, timezone
# Bound here, not looked up per call: git is not a run, and must never be
# mistaken for one by anything watching subprocess.Popen
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(DASHBOARD_DIR)
SERVER_SCRIPT = os.path.join(DASHBOARD_DIR, "server.py")

DEFAULT_GIT = "/usr/bin/git"
XCODE_SELECT = "/usr/bin/xcode-select"
CHECK_INTERVAL_SECONDS = 3 * 60 * 60
FAILED_INTERVAL_SECONDS = 30 * 60
FETCH_TIMEOUT_SECONDS = 60
GIT_TIMEOUT_SECONDS = 15
CHECK_TIMEOUT_SECONDS = 30
REAP_SECONDS = 5

PROBLEMS = ("off", "no_git", "not_git", "no_upstream", "fetch_failed", "diverged", "local_changes")

UNCHANGED = "Nothing was changed."
REFUSALS = {
    "no_git": f"Git isn't available on this Mac, so it can't update. {UNCHANGED}",
    "not_git": f"This folder isn't connected to GitHub, so it can't update. {UNCHANGED}",
    "no_upstream": f"This copy isn't following a GitHub branch, so it can't update. {UNCHANGED}",
    "diverged": f"This copy has its own changes that GitHub doesn't have, so it can't update here. {UNCHANGED}",
    "local_changes": f"This folder has local changes to project files, so it can't update. {UNCHANGED}",
}
BUSY = "An update check is going. Try again in a minute."
UP_TO_DATE = "Already up to date."
WOULD_REPLACE = "The update would replace files kept only on this Mac, so nothing was changed."
NEW_VERSION_ERROR = "The new version has an error, so nothing was changed."
GIT_FAILED = f"Git didn't answer, so the update didn't start. {UNCHANGED}"
PARTWAY = "The update stopped partway. Double-click update.command to finish it."
UNDONE = "The new version failed its startup check, so the update was undone."

def git_path():
    return os.environ.get("GOW_GIT") or DEFAULT_GIT

def load_enabled():
    """GOW_UPDATE_CHECKS is on or off. Unset, a mock run (GOW_BASE_URL) never fetches"""
    value = os.environ.get("GOW_UPDATE_CHECKS")
    if value is None:
        return os.environ.get("GOW_BASE_URL") is None
    return value == "on"

def load_interval():
    """(after a good check, after a failed one). GOW_UPDATE_INTERVAL_SECONDS is for tests"""
    try:
        value = float(os.environ.get("GOW_UPDATE_INTERVAL_SECONDS", ""))
    except ValueError:
        return CHECK_INTERVAL_SECONDS, FAILED_INTERVAL_SECONDS
    if value > 0:
        return value, value
    return CHECK_INTERVAL_SECONDS, FAILED_INTERVAL_SECONDS

def git_env():
    """This process's environment, pinned to the project's own repo and kept from prompting"""
    env = {name: value for name, value in os.environ.items()
           if name not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Otherwise a status would rewrite the index
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    # A repo in a folder above the project must never be the one used
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.realpath(BASE_DIR))
    if "GIT_SSH_COMMAND" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=20"
    return env

class GitFailed(Exception):
    """Git could not start, or ran past its time"""

class Problem(Exception):
    """A repo state that stops a check or an update, as one of PROBLEMS"""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind

class UpdateRefused(Exception):
    """An update that can't be done, with the status and message to send"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message

def stop_group(child):
    """Kill a timed-out child's whole session, so ssh and https helpers go with it"""
    # The child is unreaped here, so its group id can't have been reused
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        child.communicate(timeout=REAP_SECONDS)
    except TimeoutExpired:
        child.kill()
        child.wait()

def run_bounded(arguments, timeout, env, stdout=PIPE):
    """(exit code, stdout bytes) of a child in its own session. Raises GitFailed"""
    try:
        child = Popen(arguments, shell=False, cwd=BASE_DIR, stdin=DEVNULL, stdout=stdout, stderr=DEVNULL,
                      start_new_session=True, env=env)
    except OSError:
        raise GitFailed()

    try:
        output, _ = child.communicate(timeout=timeout)
    except TimeoutExpired:
        stop_group(child)
        raise GitFailed()
    return child.returncode, output or b""

def git(arguments, timeout=None):
    """(exit code, stdout bytes). Raises GitFailed"""
    return run_bounded([git_path(), *arguments], GIT_TIMEOUT_SECONDS if timeout is None else timeout, git_env())

def git_text(arguments):
    """Stdout of a git command that must succeed, stripped. Raises GitFailed"""
    code, output = git(arguments)
    if code != 0:
        raise GitFailed()
    return output.decode("utf-8", "replace").strip()

def git_usable():
    """False if the git to use is missing, or is Apple's shim with no developer tools behind it"""
    path = git_path()
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    if path != DEFAULT_GIT:
        return True

    # Run with no tools installed, the shim opens an install dialog every time
    try:
        code, _ = run_bounded([XCODE_SELECT, "-p"], GIT_TIMEOUT_SECONDS, git_env(), stdout=DEVNULL)
    except GitFailed:
        return False
    return code == 0

def require_git():
    if not git_usable():
        raise Problem("no_git")

def require_repo():
    """The project folder must be the top of its own repo"""
    code, output = git(["rev-parse", "--show-toplevel"])
    top = output.decode("utf-8", "replace").strip()
    if code != 0 or not top or os.path.realpath(top) != os.path.realpath(BASE_DIR):
        raise Problem("not_git")

def config_value(name):
    code, output = git(["config", "--get", name])
    return output.decode("utf-8", "replace").strip() if code == 0 else ""

def require_upstream():
    """The remote the current branch follows"""
    code, output = git(["symbolic-ref", "-q", "--short", "HEAD"])
    branch = output.decode("utf-8", "replace").strip()
    if code != 0 or not branch:
        raise Problem("no_upstream")

    remote = config_value(f"branch.{branch}.remote")
    merge = config_value(f"branch.{branch}.merge")
    if not remote or not merge or remote == ".":
        raise Problem("no_upstream")
    return remote

def has_local_changes():
    """True if a tracked file differs from HEAD. Untracked and ignored files don't count"""
    code, output = git(["status", "--porcelain", "--untracked-files=no"])
    if code != 0:
        raise GitFailed()
    return bool(output.strip())

def require_clean():
    if has_local_changes():
        raise Problem("local_changes")

def upstream_sha():
    """The full hash of the already fetched upstream commit"""
    code, output = git(["rev-parse", "--verify", "-q", "@{upstream}^{commit}"])
    sha = output.decode("utf-8", "replace").strip()
    if code != 0 or not sha:
        raise Problem("no_upstream")
    return sha

def head_sha():
    return git_text(["rev-parse", "--verify", "-q", "HEAD^{commit}"])

def require_behind():
    """How many commits HEAD is behind its upstream, refusing if it is also ahead"""
    code, output = git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    if code != 0:
        raise Problem("no_upstream")
    try:
        ahead, behind = (int(part) for part in output.decode("utf-8", "replace").split())
    except ValueError:
        raise GitFailed()
    if ahead > 0 and behind > 0:
        raise Problem("diverged")
    return behind

def version(revision):
    """{"commit": short hash, "date": "YYYY-MM-DD" in UTC} of one commit"""
    commit = git_text(["rev-parse", "--short=7", revision])
    try:
        seconds = int(git_text(["log", "-1", "--format=%ct", revision]))
    except ValueError:
        raise GitFailed()
    return {"commit": commit, "date": datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")}

def paths_between(old, new, diff_filter):
    """Paths changed between two commits, as git stores them"""
    code, output = git(["diff", "--name-only", "-z", "--no-renames", f"--diff-filter={diff_filter}", old, new])
    if code != 0:
        raise GitFailed()
    return [os.fsdecode(path) for path in output.split(b"\0") if path]

def same_entry(first, second):
    """True if two paths name the same entry on disk, as two spellings do on a case-insensitive disk"""
    try:
        a, b = os.lstat(first), os.lstat(second)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)

def is_deleted_alias(path, deleted):
    """True if what is on disk at this path is a tracked file the same update deletes.
    A case-only rename finds the old name there on a case-insensitive disk"""
    local = os.path.join(BASE_DIR, *path.split("/"))
    return any(same_entry(local, os.path.join(BASE_DIR, *old.split("/")))
               for old in deleted if old.lower() == path.lower())

def would_replace(path, deleted=()):
    """True if adding this path could touch anything that is only on this Mac.
    deleted: the tracked paths the same update removes"""
    parts = path.split("/")
    for depth in range(1, len(parts)):
        parent = os.path.join(BASE_DIR, *parts[:depth])
        if os.path.lexists(parent) and (os.path.islink(parent) or not os.path.isdir(parent)):
            return True

    if os.path.lexists(os.path.join(BASE_DIR, *parts)) and not is_deleted_alias(path, deleted):
        return True

    code, _ = git(["check-ignore", "-q", "--", path])
    if code not in (0, 1):
        raise GitFailed()
    return code == 0

def has_error(target, path):
    """True if this interpreter can't compile the file as it is in the target commit"""
    code, source = git(["show", f"{target}:{path}"])
    if code != 0:
        raise GitFailed()
    try:
        # A warning is not an error, and would print the new code to the log
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(source, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError):
        return True
    return False

def passes_startup_check():
    """True if the code now on disk starts: the server's own --check, in a child"""
    try:
        code, _ = run_bounded([sys.executable, SERVER_SCRIPT, "--check"], CHECK_TIMEOUT_SECONDS,
                              dict(os.environ), stdout=DEVNULL)
    except GitFailed:
        return False
    return code == 0

class Updater:
    """Checks on its own thread. status() only takes a short lock, never the git one"""

    def __init__(self):
        self.enabled = load_enabled()
        self.good_interval, self.failed_interval = load_interval()

        # state_lock guards the fields only and is never held across git. git_lock
        # is held for any git work, so a check and an update never overlap
        self.state_lock = threading.Lock()
        self.git_lock = threading.Lock()
        self.wake = threading.Event()
        self.current = None
        self.available = None
        self.behind = 0
        self.checking = True
        self.checked_at = None
        self.problem = None if self.enabled else "off"
        # Set once an update has gone in: this process is about to be replaced
        self.closed = False
        self.thread = threading.Thread(target=self.run, name="gow-update", daemon=True)

    def start(self):
        self.thread.start()

    def check_soon(self):
        """Wake the thread for a check. Returns at once"""
        if not self.enabled:
            return
        with self.state_lock:
            self.checking = True
        self.wake.set()

    def status(self):
        """The current state, with fixed fields only: no URL, path or git output"""
        with self.state_lock:
            age = None if self.checked_at is None else round(time.monotonic() - self.checked_at)
            return {
                "enabled": self.enabled,
                "current": dict(self.current) if self.current else None,
                "available": dict(self.available) if self.available else None,
                "behind": self.behind,
                "checking": self.checking,
                "checked_seconds_ago": age,
                "problem": self.problem,
            }

    def inspect(self):
        """One pass: (problem, current, available, behind). Fetches only with checks on"""
        current = None
        try:
            require_git()
            require_repo()
            current = version("HEAD")
            # Shown at once, not after a fetch that may take a minute
            with self.state_lock:
                self.current = current
            if not self.enabled:
                return "off", current, None, 0

            remote = require_upstream()
            try:
                code, _ = git(["fetch", "--quiet", "--no-tags", "--", remote], timeout=FETCH_TIMEOUT_SECONDS)
            except GitFailed:
                code = None
            if code != 0:
                return "fetch_failed", current, None, 0

            # Noted rather than ending the pass, so an edited file still shows what's waiting
            changed = has_local_changes()
            behind = require_behind()
            available = version(upstream_sha()) if behind > 0 else None
        except Problem as e:
            return e.kind, current, None, 0
        except GitFailed:
            return "no_git", current, None, 0
        return ("local_changes" if changed else None), current, available, behind

    def check(self):
        """Run one pass and record it, returning its problem"""
        with self.state_lock:
            self.checking = True

        with self.git_lock:
            if self.closed:
                return None
            try:
                problem, current, available, behind = self.inspect()
            except Exception:
                problem, current, available, behind = "no_git", None, None, 0
        if not self.enabled:
            problem = "off"

        with self.state_lock:
            self.problem = problem
            self.current = current
            self.available = available
            self.behind = behind
            if self.enabled:
                self.checked_at = time.monotonic()
            # A check asked for during this one runs next, so it stays checking
            self.checking = self.enabled and self.wake.is_set()
        return problem

    def run(self):
        while True:
            self.wake.clear()
            problem = self.check()
            if not self.enabled:
                return
            self.wake.wait(self.good_interval if problem is None else self.failed_interval)

    def apply(self):
        """Fast-forward to the fetched upstream and confirm the new code starts.
        Returns (old version, new version). Raises UpdateRefused, having changed nothing
        unless the message says otherwise"""
        if not self.git_lock.acquire(blocking=False):
            raise UpdateRefused(409, BUSY)
        try:
            return self.apply_locked()
        finally:
            self.git_lock.release()

    def apply_locked(self):
        try:
            require_git()
            require_repo()
            require_upstream()
            require_clean()
            if require_behind() == 0:
                raise UpdateRefused(409, UP_TO_DATE)

            old = head_sha()
            target = upstream_sha()
            old_version = version(old)
            new_version = version(target)

            # A file kept only on this Mac, such as users.txt or .venv, must never be replaced
            deleted = paths_between(old, target, "D")
            if any(would_replace(path, deleted) for path in paths_between(old, target, "A")):
                raise UpdateRefused(409, WOULD_REPLACE)

            # Compiled in memory by the interpreter that will run it: nothing is written
            changed = [path for path in paths_between(old, target, "AMR") if path.endswith(".py")]
            if any(has_error(target, path) for path in changed):
                raise UpdateRefused(409, NEW_VERSION_ERROR)
        except Problem as e:
            raise UpdateRefused(409, REFUSALS[e.kind])
        except GitFailed:
            raise UpdateRefused(500, GIT_FAILED)

        try:
            code, _ = git(["merge", "--ff-only", "--no-overwrite-ignore", "--quiet", target])
            merged = code == 0 and head_sha() == target
        except GitFailed:
            merged = False
        if not merged:
            raise UpdateRefused(500, PARTWAY)

        if not passes_startup_check():
            try:
                code, _ = git(["reset", "--keep", old])
                undone = code == 0 and head_sha() == old
            except GitFailed:
                undone = False
            raise UpdateRefused(500, UNDONE if undone else PARTWAY)

        with self.state_lock:
            self.closed = True
            self.current = new_version
            self.available = None
            self.behind = 0
            self.problem = None
        return old_version, new_version
