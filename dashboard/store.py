#!/usr/bin/env python3
"""
Game of War - Run event reader
Follows the event file and derives everything the dashboard shows. The file is
treated as untrusted: a line that fails the recorder's own checks is skipped.
"""

import json
import os
import stat
import threading
import time
from datetime import datetime, timedelta, timezone

from dashboard import events

JEWELS_PER_CLAIM = 75
MAX_LINE_BYTES = 64 * 1024
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_LOG_LINES = 200
MAX_RUNS = 400
MAX_ACCOUNT_ROWS = 5000
RECENT_RUNS = 6
CHART_DAYS = 14
QUIET_WARNING_SECONDS = 120

REASON_LABELS = {
    "page_load_failed": "Page did not load",
    "login_rejected": "Login rejected",
    "no_claim_button": "No claim button",
    "claim_no_request": "Click sent no claim",
    "claim_no_response": "No claim response",
    "claim_rejected": "Claim rejected",
    "claim_unreadable": "Unreadable claim response",
    "switch_failed": "Character switch failed",
    "deadline": "Stopped after 15 minutes",
    "error": "Unexpected error",
    "stopped": "Stopped before finishing",
}

def utc_date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date()

def parse_line(raw):
    """One event from one line of the file, or None if anything about it is wrong"""
    if len(raw) > MAX_LINE_BYTES:
        return None

    def refuse(name):
        raise ValueError(name)

    try:
        event = json.loads(raw, parse_constant=refuse)
        return event if events.validate_event(event) else None
    except (ValueError, OverflowError, RecursionError):
        return None

class EventStore:
    """Everything known from the event files, safe to use from the server's threads"""

    def __init__(self, path=None):
        self.path = path if path is not None else events.events_path()
        self.lock = threading.Lock()
        self.offsets = {}
        self.runs = {}
        self.days = {}

    def read_file(self, path):
        # Keyed by inode, so a rotated file carries on from where it was read to,
        # under its new name, and the new file starts from zero
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return

            offset = self.offsets.get(info.st_ino, 0)
            if offset > info.st_size:
                offset = 0

            os.lseek(fd, offset, os.SEEK_SET)
            data = os.read(fd, MAX_READ_BYTES)
        finally:
            os.close(fd)

        complete, newline, rest = data.rpartition(b"\n")
        if not newline:
            # One line longer than a whole read would otherwise stop the file here
            self.offsets[info.st_ino] = offset + (len(data) if len(data) == MAX_READ_BYTES else 0)
            return

        self.offsets[info.st_ino] = offset + len(complete) + 1
        for raw in complete.split(b"\n"):
            event = parse_line(raw)
            if event is not None:
                self.apply(event)

    def refresh(self):
        if self.path is None:
            return
        self.read_file(f"{self.path}.1")
        self.read_file(self.path)

    def apply(self, event):
        name = event["event"]
        run = self.runs.get(event["run"])

        if name == "run_start":
            if run is None:
                self.runs[event["run"]] = {
                    "id": event["run"],
                    "source": event["source"],
                    "script": event["script"],
                    "concurrency": event["concurrency"],
                    "total": event["total"],
                    "started": event["ts"],
                    "ended": None,
                    "status": None,
                    "crashed": False,
                    "last_event": event["ts"],
                    "accounts": {},
                    "log": [],
                    "jewels": 0,
                }
                while len(self.runs) > MAX_RUNS:
                    del self.runs[next(iter(self.runs))]
            return

        if run is None:
            return

        run["last_event"] = max(run["last_event"], event["ts"])

        if name == "run_end":
            run["ended"] = event["ts"]
            run["status"] = event["status"]
            run["crashed"] = False
            return

        account = run["accounts"].setdefault(event["line"], {
            "line": event["line"],
            "label": None,
            "stage": None,
            "result": None,
            "reason": None,
            "status": None,
            "characters": 0,
            "claimed": 0,
            "already": 0,
            "seconds": None,
            "order": len(run["accounts"]),
            "changed": event["ts"],
        })
        account["changed"] = event["ts"]

        if name == "account_start":
            account["label"] = event["label"]
            self.add_log(run, event["ts"], "start", f"{event['label']} started")
        elif name == "stage":
            if account["result"] is None:
                account["stage"] = event["stage"]
                self.add_log(run, event["ts"], "stage", f"{account['label'] or 'Account'} {stage_word(event['stage'])}")
        elif name == "reason":
            account["reason"] = event["reason"]
            account["status"] = event.get("status")
        elif name == "account_end" and account["result"] is None:
            self.finish_account(run, account, event)

    def finish_account(self, run, account, event):
        account["label"] = event["label"]
        account["characters"] = event["characters"]
        account["claimed"] = event["claimed"]
        account["already"] = event["already"]
        account["seconds"] = event["seconds"]
        account["success"] = event["success"]
        account["interrupted"] = event["interrupted"]

        if not event["success"]:
            account["result"] = "failed"
            if account["reason"] is None:
                account["reason"] = "stopped" if event["interrupted"] else "error"
        elif event["claimed"] > 0:
            account["result"] = "claimed"
        else:
            account["result"] = "already"

        jewels = event["claimed"] * JEWELS_PER_CLAIM
        run["jewels"] += jewels
        day = utc_date(event["ts"]).isoformat()
        self.days[day] = self.days.get(day, 0) + jewels

        label = event["label"]
        if account["result"] == "failed":
            status = f" {account['status']}" if account["status"] else ""
            text = f"{label} failed: {REASON_LABELS[account['reason']].lower()}{status}"
            if jewels:
                text += f" (+{jewels} kept)"
        elif jewels:
            text = f"{label} +{jewels}"
        else:
            text = f"{label} already claimed"
        self.add_log(run, event["ts"], account["result"], text)

    def add_log(self, run, ts, kind, text):
        run["log"].append({"ts": ts, "kind": kind, "text": text})
        del run["log"][:-MAX_LOG_LINES]

    def lock_file(self, run):
        return events.lock_path(self.path, run["id"])

    def update_liveness(self):
        """Settle every run with no run_end: alive if its lock is held, otherwise crashed"""
        for run in list(self.runs.values()):
            if run["status"] is not None or run["crashed"]:
                continue

            target = self.lock_file(run)
            if events.lock_held(target):
                continue

            # It may have ended, and dropped its lock, since the file was read
            self.refresh()
            if run["status"] is None:
                run["crashed"] = True
                try:
                    os.unlink(target)
                except OSError:
                    pass

    def is_live(self, run):
        return run["status"] is None and not run["crashed"]

    def live_runs(self):
        return [run for run in self.runs.values() if self.is_live(run)]

    def any_live(self):
        """True if a run is live, wherever it was started"""
        with self.lock:
            self.refresh()
            self.update_liveness()
            return bool(self.live_runs())

    def has_run_for_pid(self, pid):
        """True once a run from this process has appeared"""
        with self.lock:
            self.refresh()
            return any(run["id"].endswith(f"-{pid}") for run in self.runs.values())

    def snapshot(self):
        """The state of every panel, built from events alone"""
        with self.lock:
            now = time.time()
            self.refresh()
            self.update_liveness()

            live = self.live_runs()
            runs = list(self.runs.values())
            current = live[-1] if live else (runs[-1] if runs else None)
            ended = [run for run in runs if not self.is_live(run)]

            return {
                "status": self.status_of(current),
                "live": bool(live),
                "live_count": len(live),
                "run": self.run_summary(current, now),
                "totals": self.totals(current, now),
                "slots": self.slots(current),
                "days": self.chart_days(now),
                "reasons": self.reasons(current),
                "accounts": self.account_rows(current),
                "log": list(reversed(current["log"])) if current else [],
                "recent_runs": [self.recent_run(run, now) for run in reversed(runs[-RECENT_RUNS:])],
                "retry_lines": self.retry_lines(ended[-1]) if ended else [],
                "quiet_seconds": round(max(0, now - current["last_event"])) if current and self.is_live(current) else None,
                "quiet_warning_seconds": QUIET_WARNING_SECONDS,
            }

    def status_of(self, run):
        if run is None:
            return "idle"
        if self.is_live(run):
            return "running"
        if run["crashed"]:
            return "crashed"
        return "stopped" if run["status"] == "interrupted" else "finished"

    def run_end_time(self, run, now):
        """When a run ended, or now if it is live, or its last event if it crashed"""
        return run["ended"] or (now if self.is_live(run) else run["last_event"])

    def claim_count(self, run):
        return sum(account["claimed"] for account in run["accounts"].values())

    def seconds_per_claim(self, run, now):
        """The run's real pace in seconds per new claim, or None with nothing to divide"""
        claims = self.claim_count(run)
        elapsed = self.run_end_time(run, now) - run["started"]
        if claims == 0 or elapsed <= 0:
            return None
        return max(round(elapsed / claims, 1), 0.1)

    def run_summary(self, run, now):
        if run is None:
            return None
        return {
            "id": run["id"],
            "source": run["source"],
            "script": run["script"],
            "concurrency": run["concurrency"],
            "started": run["started"],
            "ended": run["ended"],
            "seconds": round(self.run_end_time(run, now) - run["started"]),
            "claims": self.claim_count(run),
            "seconds_per_claim": self.seconds_per_claim(run, now),
        }

    def counts(self, run):
        results = [account["result"] for account in run["accounts"].values()]
        return {name: results.count(name) for name in ("claimed", "already", "failed")}

    def totals(self, run, now):
        if run is None:
            return None

        counts = self.counts(run)
        done = sum(counts.values())
        accounts = run["accounts"].values()
        seconds_left = None
        seconds_per_account = None

        if done and self.is_live(run):
            seconds_per_account = (now - run["started"]) / done
            seconds_left = round(seconds_per_account * max(run["total"] - done, 0))
            seconds_per_account = round(seconds_per_account)

        return {
            **counts,
            "jewels": run["jewels"],
            "characters": sum(account["characters"] for account in accounts),
            "done": done,
            "total": run["total"],
            "unfinished": 0 if self.is_live(run) else len(run["accounts"]) - done,
            "seconds_left": seconds_left,
            "seconds_per_account": seconds_per_account,
        }

    def slots(self, run):
        if run is None or not self.is_live(run):
            return []

        working = [account for account in run["accounts"].values() if account["result"] is None]
        working.sort(key=lambda account: account["order"])
        return [
            {"label": account["label"], "line": account["line"], "stage": account["stage"]}
            for account in working[:run["concurrency"]]
        ]

    def chart_days(self, now):
        today = utc_date(now)
        days = []
        for back in range(CHART_DAYS - 1, -1, -1):
            day = today - timedelta(days=back)
            days.append({
                "date": day.isoformat(),
                "day": day.day,
                "jewels": self.days.get(day.isoformat(), 0),
                "today": back == 0,
            })
        return days

    def reasons(self, run):
        if run is None:
            return []

        found = {}
        for account in run["accounts"].values():
            if account["result"] == "failed":
                found[account["reason"]] = found.get(account["reason"], 0) + 1

        ordered = sorted(found.items(), key=lambda item: (-item[1], item[0]))
        return [{"reason": name, "label": REASON_LABELS[name], "count": count} for name, count in ordered]

    def account_rows(self, run):
        if run is None:
            return []

        live = self.is_live(run)
        accounts = sorted(run["accounts"].values(), key=lambda account: account["changed"], reverse=True)
        return [{
            "line": account["line"],
            "label": account["label"] or "?***",
            "characters": account["characters"],
            "result": account["result"] or ("running" if live else "unfinished"),
            "jewels": account["claimed"] * JEWELS_PER_CLAIM,
            "seconds": account["seconds"],
            "reason": REASON_LABELS.get(account["reason"]),
        } for account in accounts[:MAX_ACCOUNT_ROWS]]

    def recent_run(self, run, now):
        counts = self.counts(run)
        return {
            "id": run["id"],
            "date": utc_date(run["started"]).strftime("%b %d"),
            "jewels": run["jewels"],
            "done": sum(counts.values()),
            "total": run["total"],
            "seconds": round(self.run_end_time(run, now) - run["started"]),
            "claims": self.claim_count(run),
            "seconds_per_claim": self.seconds_per_claim(run, now),
            "source": run["source"],
            "status": self.status_of(run),
        }

    def retry_lines(self, run):
        """Lines worth another run: not successful, or with characters still unclaimed"""
        lines = []
        for account in run["accounts"].values():
            if account["result"] is None or account["line"] < 1:
                continue
            if not account["success"] or account["characters"] > account["claimed"] + account["already"]:
                lines.append(account["line"])
        return sorted(lines)

def stage_word(stage):
    return "sign-in" if stage == "signin" else stage
