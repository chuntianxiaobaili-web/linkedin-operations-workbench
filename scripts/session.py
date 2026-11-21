"""Persistent local task queue. Commands only record work and outcomes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4


KINDS = ("research", "content", "knowledge", "record", "receipt", "manual")
DURATION_TOKEN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(小时|分钟|秒钟|时|分|秒|[hms])")
UNIT_SECONDS = {"小时": 3600, "时": 3600, "h": 3600, "分钟": 60, "分": 60, "m": 60,
                "秒钟": 1, "秒": 1, "s": 1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS session_runs (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    scope TEXT,
    started_at REAL NOT NULL,
    deadline REAL,
    ended_at REAL,
    end_reason TEXT,
    phase TEXT NOT NULL,
    phase_at REAL NOT NULL,
    phase_until REAL,
    phase_task_id TEXT
);
CREATE TABLE IF NOT EXISTS session_tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES session_runs(id),
    task_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL,
    due REAL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    claimed_at REAL,
    finished_at REAL,
    result TEXT,
    reason TEXT,
    UNIQUE (run_id, task_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS session_one_running_task
    ON session_tasks(run_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS session_task_status
    ON session_tasks(run_id, status, priority, due);
CREATE TABLE IF NOT EXISTS session_spans (
    run_id TEXT NOT NULL REFERENCES session_runs(id),
    task_id TEXT REFERENCES session_tasks(id),
    phase TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    phase_until REAL,
    worked_seconds REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES session_runs(id),
    task_id TEXT REFERENCES session_tasks(id),
    event TEXT NOT NULL,
    at REAL NOT NULL,
    detail TEXT
);
"""


class SessionError(ValueError):
    """A command rejected without changing the task state."""


def duration_seconds(raw):
    raw = raw.strip().lower()
    total = 0.0
    position = 0
    for match in DURATION_TOKEN.finditer(raw):
        if raw[position:match.start()].strip():
            raise argparse.ArgumentTypeError("Expected a duration such as 2h, 90m or 2小时30分钟.")
        total += float(match[1]) * UNIT_SECONDS[match[2]]
        position = match.end()
    if raw[position:].strip() or not math.isfinite(total) or total <= 0:
        raise argparse.ArgumentTypeError("Expected a positive duration such as 2h, 90m or 2小时30分钟.")
    return total


def parse_timestamp(raw):
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise argparse.ArgumentTypeError("Use a timezone-aware ISO date, e.g. 2026-11-19T18:00:00+08:00.") from exc


def date_text(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


def require_value(value, label):
    if not value or not value.strip():
        raise SessionError(f"{label} must contain text.")
    return value.strip()


class SessionStore:
    def __init__(self, db, clock=time.time):
        path = Path(db).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.connection.close()

    @contextmanager
    def transaction(self, *, write=True):
        self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        with self.connection:
            yield

    def run(self, run_id):
        row = self.connection.execute("SELECT * FROM session_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise SessionError(f"Unknown run ID: {run_id}")
        return row

    def task(self, run_id, task_id):
        row = self.connection.execute(
            "SELECT * FROM session_tasks WHERE run_id = ? AND id = ?", (run_id, task_id)
        ).fetchone()
        if row is None:
            raise SessionError(f"Task {task_id} does not belong to run {run_id}.")
        return row

    def event(self, run_id, event, now, *, task_id=None, detail=None):
        self.connection.execute(
            "INSERT INTO session_events (run_id, event, at, task_id, detail) VALUES (?, ?, ?, ?, ?)",
            (run_id, event, now, task_id, detail),
        )

    @staticmethod
    def run_status(run, now):
        if run["ended_at"] is not None:
            return "closed"
        if run["deadline"] is not None and now >= run["deadline"]:
            return "expired"
        return "open"

    def require_open(self, run, now):
        if self.run_status(run, now) != "open":
            raise SessionError("This run is closed or past its cutoff. Existing outcomes may still be recorded.")

    def queue(self, run, now):
        """Get the current decision without claiming or modifying a task."""
        state = self.run_status(run, now)
        items = self.connection.execute(
            """SELECT * FROM session_tasks WHERE run_id = ? AND status != 'done'
               ORDER BY priority DESC, created_at, rowid""",
            (run["id"],),
        ).fetchall()
        running = next((item for item in items if item["status"] == "running"), None)
        if state != "open":
            return state, running, None
        if running:
            return "in_progress", running, None
        queued = [item for item in items if item["status"] == "queued"]
        ready = next((item for item in queued if item["due"] is None or item["due"] <= now), None)
        if ready:
            return "ready", ready, None
        next_due = min((item["due"] for item in queued), default=None)
        if next_due is not None or any(item["status"] == "waiting" for item in items):
            return "waiting", None, next_due
        if any(item["status"] == "needs_reconcile" for item in items):
            return "needs_reconcile", None, None
        if items:
            return "blocked", None, None
        return "empty", None, None

    @staticmethod
    def task_data(row):
        if row is None:
            return None
        task = dict(row)
        for key in ("due", "created_at", "claimed_at", "finished_at"):
            task[key] = date_text(task[key])
        return task

    def next_data(self, run, now):
        state, task, next_due = self.queue(run, now)
        return {
            "run_id": run["id"],
            "status": state,
            "deadline": date_text(run["deadline"]),
            "next_due": date_text(next_due),
            "task": self.task_data(task),
        }

    def start(self, goal, *, duration=None, scope=None):
        goal = require_value(goal, "Goal")
        scope = require_value(scope, "Scope") if scope is not None else None
        with self.transaction():
            now = self.clock()
            deadline = now + duration if duration is not None else None
            # Check timestamp range before writing a run whose dates cannot be reported.
            try:
                if deadline is not None and deadline <= now:
                    raise ValueError("duration must be positive")
                deadline_text = date_text(deadline)
            except (ValueError, OverflowError, OSError) as exc:
                raise SessionError("Duration creates an invalid deadline.") from exc
            run_id = uuid4().hex
            self.connection.execute(
                """INSERT INTO session_runs (id, goal, scope, started_at, deadline, phase, phase_at)
                   VALUES (?, ?, ?, ?, ?, 'idle', ?)""",
                (run_id, goal, scope, now, deadline, now),
            )
            self.event(run_id, "started", now)
            return {"run_id": run_id, "goal": goal, "scope": scope,
                    "started_at": date_text(now), "deadline": deadline_text}

    def finish_span(self, run, now, *, worked=0.0, phase=None):
        since = run["phase_at"]
        elapsed = max(0.0, now - since)
        if worked < 0 or not math.isfinite(worked) or worked > elapsed:
            raise SessionError("Measured work seconds must be between zero and the elapsed claim interval.")
        if elapsed:
            self.connection.execute(
                """INSERT INTO session_spans
                   (run_id, task_id, phase, started_at, ended_at, phase_until, worked_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run["id"], run["phase_task_id"], phase or run["phase"], since, now,
                 run["phase_until"], worked),
            )

    def sync_phase(self, run, now, *, worked=0.0, interrupted=False, closing=False):
        status, task, until = self.queue(run, now)
        if closing:
            phase, task_id, until = "idle", None, None
        elif status == "in_progress" or (status == "expired" and task):
            phase, task_id, until = "work", task["id"], None
        elif status == "waiting":
            phase, task_id = "waiting", None
        elif status in ("blocked", "needs_reconcile"):
            phase, task_id, until = "blocked", None, None
        else:
            phase, task_id, until = "idle", None, None
        if not closing and not interrupted and not worked and (
            phase, task_id, until
        ) == (run["phase"], run["phase_task_id"], run["phase_until"]):
            return
        if worked and run["phase"] != "work":
            raise SessionError("Work time can only be recorded on a running task.")
        self.finish_span(run, now, worked=worked, phase="interrupted" if interrupted else None)
        self.connection.execute(
            """UPDATE session_runs SET phase = ?, phase_at = ?, phase_until = ?, phase_task_id = ?
               WHERE id = ?""",
            (phase, max(now, run["phase_at"]), until, task_id, run["id"]),
        )

    def add(self, run_id, *, kind, title, priority=50, due=None, key=None):
        if kind not in KINDS:
            raise SessionError(f"Unknown task kind: {kind}")
        if not 0 <= priority <= 100:
            raise SessionError("Priority must be between 0 and 100.")
        title = require_value(title, "Task title")
        task_key = require_value(key, "Task key") if key is not None else f"{kind}:{title.casefold()}"
        with self.transaction():
            now = self.clock()
            run = self.run(run_id)
            existing = self.connection.execute(
                "SELECT * FROM session_tasks WHERE run_id = ? AND task_key = ?", (run_id, task_key),
            ).fetchone()
            if existing:
                if tuple(existing[field] for field in ("kind", "title", "priority", "due")) != (
                    kind, title, priority, due
                ):
                    raise SessionError("The task key is already bound to different task details.")
                return {"task": self.task_data(existing), "existing": True}
            self.require_open(run, now)
            task_id = uuid4().hex
            self.connection.execute(
                """INSERT INTO session_tasks
                   (id, run_id, task_key, kind, title, priority, due, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (task_id, run_id, task_key, kind, title, priority, due, now),
            )
            self.event(run_id, "added", now, task_id=task_id)
            self.sync_phase(run, now)
            return {"task": self.task_data(self.task(run_id, task_id)), "existing": False}

    def next(self, run_id):
        with self.transaction(write=False):
            return self.next_data(self.run(run_id), self.clock())

    def claim_ready(self, run, now, task_id=None, *, update_phase=True):
        run_id = run["id"]
        state, candidate, _ = self.queue(run, now)
        if state != "ready":
            return self.next_data(run, now)
        if task_id is not None:
            candidate = self.task(run_id, task_id)
            if candidate["status"] != "queued" or (candidate["due"] is not None and now < candidate["due"]):
                raise SessionError("The requested task is not ready to claim.")
        task_id = candidate["id"]
        self.connection.execute(
            """UPDATE session_tasks SET status = 'running', claimed_at = ?, reason = NULL
               WHERE id = ?""", (now, task_id),
        )
        self.event(run_id, "claimed", now, task_id=task_id)
        if update_phase:
            self.sync_phase(run, now)
        return {"run_id": run_id, "status": "claimed", "deadline": date_text(run["deadline"]),
                "task": self.task_data(self.task(run_id, task_id))}

    def claim(self, run_id, task_id=None):
        with self.transaction():
            return self.claim_ready(self.run(run_id), self.clock(), task_id)

    def done(self, run_id, task_id, *, result, worked=0.0):
        result = require_value(result, "A verifiable result")
        with self.transaction():
            now = self.clock()
            run = self.run(run_id)
            task = self.task(run_id, task_id)
            if task["status"] == "done" and task["result"] == result:
                return self.task_data(task)
            if task["status"] not in ("running", "needs_reconcile"):
                raise SessionError("Only active tasks or tasks awaiting reconciliation can be completed.")
            if worked and task["status"] != "running":
                raise SessionError("Interrupted work time is unmeasured. Only its outcome can be reconciled.")
            self.connection.execute(
                """UPDATE session_tasks SET status = 'done', finished_at = ?, result = ?, reason = NULL
                   WHERE id = ?""",
                (now, result, task_id),
            )
            self.event(run_id, "done", now, task_id=task_id, detail=result)
            if run["ended_at"] is None:
                self.sync_phase(run, now, worked=worked)
            return self.task_data(self.task(run_id, task_id))

    def mark_blocked(self, run, task_id, now, *, reason, waiting, worked, switch):
        task = self.task(run["id"], task_id)
        if task["status"] not in ("queued", "running"):
            raise SessionError("Only a queued or running task can move to blocked or waiting.")
        if switch and task["status"] != "running":
            raise SessionError("A switch requires the current running task.")
        if worked and task["status"] != "running":
            raise SessionError("Work time can only be recorded on a running task.")
        status = "waiting" if waiting else "blocked"
        self.connection.execute(
            "UPDATE session_tasks SET status = ?, reason = ? WHERE id = ?", (status, reason, task_id),
        )
        self.event(run["id"], status, now, task_id=task_id, detail=reason)
        if switch:
            next_task = self.claim_ready(run, now, update_phase=False)
        self.sync_phase(run, now, worked=worked)
        result = {"task": self.task_data(self.task(run["id"], task_id))}
        if switch:
            result["next"] = next_task
        return result

    def block(self, run_id, task_id, *, reason, waiting=False, worked=0.0, switch=False):
        reason = require_value(reason, "Reason")
        with self.transaction():
            now = self.clock()
            run = self.run(run_id)
            if self.run_status(run, now) == "closed":
                raise SessionError("Run is already closed.")
            return self.mark_blocked(
                run, task_id, now, reason=reason, waiting=waiting, worked=worked, switch=switch,
            )

    def requeue(self, run_id, task_id, *, reason, due=None, reconciled=False):
        reason = require_value(reason, "Recovery evidence")
        with self.transaction():
            now = self.clock()
            run = self.run(run_id)
            self.require_open(run, now)
            task = self.task(run_id, task_id)
            if task["status"] not in ("blocked", "waiting", "needs_reconcile"):
                raise SessionError("Only blocked, waiting or interrupted tasks can be requeued.")
            if task["status"] == "needs_reconcile" and not reconciled:
                raise SessionError("Check the old outcome first. Use done for a confirmed result or --reconciled after recovery checks.")
            self.connection.execute(
                """UPDATE session_tasks SET status = 'queued', due = ?, claimed_at = NULL, reason = NULL
                   WHERE id = ?""", (due, task_id),
            )
            self.event(run_id, "requeued", now, task_id=task_id, detail=reason)
            self.sync_phase(run, now)
            return self.task_data(self.task(run_id, task_id))

    def resume(self, run_id, *, reason="Resumed from persisted state."):
        reason = require_value(reason, "Recovery reason")
        with self.transaction():
            now = self.clock()
            run = self.run(run_id)
            if self.run_status(run, now) == "closed":
                raise SessionError("A closed run cannot be resumed.")
            active = self.connection.execute(
                "SELECT id FROM session_tasks WHERE run_id = ? AND status = 'running'", (run_id,),
            ).fetchone()
            if active:
                self.connection.execute(
                    "UPDATE session_tasks SET status = 'needs_reconcile', reason = ? WHERE id = ?",
                    (reason, active["id"]),
                )
                self.event(run_id, "interrupted", now, task_id=active["id"], detail=reason)
                self.sync_phase(run, now, interrupted=True)
            self.event(run_id, "resumed", now, detail=reason)
            return self.next_data(run, now)

    def report_data(self, run, now, *, events):
        run_id = run["id"]
        endpoint = now if run["ended_at"] is None else run["ended_at"]
        elapsed = max(0.0, endpoint - run["started_at"])
        deadline = run["deadline"]
        spans = [dict(row) for row in self.connection.execute(
            "SELECT * FROM session_spans WHERE run_id = ?", (run_id,),
        )]
        if run["ended_at"] is None:
            spans.append({"phase": run["phase"], "started_at": run["phase_at"], "ended_at": endpoint,
                          "phase_until": run["phase_until"], "worked_seconds": 0.0})
        totals = {phase: 0.0 for phase in (
            "active_work", "unmeasured_work", "waiting", "blocked", "idle", "interrupted",
        )}
        for span in spans:
            started, ended = span["started_at"], span["ended_at"]
            length = max(0.0, ended - started)
            phase = span["phase"]
            if phase == "work":
                worked = span["worked_seconds"]
                totals["active_work"] += worked
                totals["unmeasured_work"] += length - worked
            elif phase in ("waiting", "blocked"):
                stops = [ended]
                if span["phase_until"] is not None:
                    stops.append(span["phase_until"])
                if deadline is not None:
                    stops.append(deadline)
                observed = max(0.0, min(stops) - started)
                totals[phase] += observed
                totals["idle"] += length - observed
            else:
                totals[phase] += length
        tasks = [self.task_data(row) for row in self.connection.execute(
            "SELECT * FROM session_tasks WHERE run_id = ? ORDER BY created_at, rowid", (run_id,),
        )]
        counts = {state: sum(task["status"] == state for task in tasks) for state in (
            "queued", "running", "blocked", "waiting", "needs_reconcile", "done",
        )}
        in_window = elapsed if deadline is None else max(0.0, min(deadline, endpoint) - run["started_at"])
        info = {
            **self.next_data(run, now),
            "goal": run["goal"],
            "scope": run["scope"],
            "started_at": date_text(run["started_at"]),
            "ended_at": date_text(run["ended_at"]),
            "end_reason": run["end_reason"],
            "elapsed_seconds": round(elapsed, 3),
            "window_elapsed_seconds": round(in_window, 3),
            "after_deadline_seconds": round(elapsed - in_window, 3),
            "time_seconds": {phase: round(seconds, 3) for phase, seconds in totals.items()},
            "counts": counts,
            "tasks": tasks,
        }
        if events:
            info["events"] = [
                {**dict(row), "at": date_text(row["at"])}
                for row in self.connection.execute(
                    "SELECT * FROM session_events WHERE run_id = ? ORDER BY id", (run_id,),
                )
            ]
        return info

    def report(self, run_id, *, events=False):
        with self.transaction(write=False):
            return self.report_data(self.run(run_id), self.clock(), events=events)

    def close(self, run_id, *, reason="Run closed.", events=False):
        reason = require_value(reason, "Closing reason")
        with self.transaction():
            now = self.clock()
            run = self.run(run_id)
            if run["ended_at"] is not None:
                return self.report_data(run, now, events=events)
            if self.connection.execute(
                "SELECT 1 FROM session_tasks WHERE run_id = ? AND status = 'running'", (run_id,),
            ).fetchone():
                raise SessionError("Account for the running task with done, block or resume before closing.")
            self.sync_phase(run, now, closing=True)
            self.connection.execute(
                "UPDATE session_runs SET ended_at = ?, end_reason = ? WHERE id = ?",
                (now, reason, run_id),
            )
            self.event(run_id, "closed", now, detail=reason)
            return self.report_data(self.run(run_id), now, events=events)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Local SQLite state file. Use the same file to resume.")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Create a run and optionally fix its deadline.")
    start.add_argument("--goal", required=True)
    start.add_argument("--scope")
    start.add_argument("--duration", type=duration_seconds)

    def run_command(name, help_text, *, task=False):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("run_id")
        if task:
            command.add_argument("task_id")
        return command

    add = run_command("add", "Add a bounded research, content, knowledge, record, receipt or manual task.")
    add.add_argument("--kind", required=True, choices=KINDS)
    add.add_argument("--title", required=True)
    add.add_argument("--priority", type=int, default=50)
    add.add_argument("--due", type=parse_timestamp)
    add.add_argument("--key")

    run_command("next", "Read the queue decision without claiming.")
    claim = run_command("claim", "Claim one ready task. No task starts after the cutoff.")
    claim.add_argument("--task", dest="task_id")

    done = run_command("done", "Record a verified output or reconciled outcome.", task=True)
    done.add_argument("--result", required=True)
    done.add_argument("--worked-seconds", type=float, default=0.0)

    for name, help_text in (
        ("block", "Record a dependency or external wait."),
        ("switch", "Block the running task and claim the next ready local task."),
    ):
        action = run_command(name, help_text, task=True)
        action.add_argument("--reason", required=True)
        action.add_argument("--waiting", action="store_true")
        action.add_argument("--worked-seconds", type=float, default=0.0)

    requeue = run_command("requeue", "Return a task to the queue after checking its previous outcome.", task=True)
    requeue.add_argument("--reason", required=True)
    requeue.add_argument("--due", type=parse_timestamp)
    requeue.add_argument("--reconciled", action="store_true")

    resume = run_command("resume", "Resume the same run and flag an interrupted task for reconciliation.")
    resume.add_argument("--reason", default="Resumed from persisted state.")

    report = run_command("report", "Summarize tasks, outputs and observed time.")
    report.add_argument("--events", action="store_true")
    close = run_command("close", "Close and summarize the run.")
    close.add_argument("--reason", default="Run closed.")
    close.add_argument("--events", action="store_true")
    return parser


def main(argv=None):
    args = vars(build_parser().parse_args(argv))
    db, command = args.pop("db"), args.pop("command")
    if "worked_seconds" in args:
        args["worked"] = args.pop("worked_seconds")
    try:
        with SessionStore(db) as store:
            if command == "switch":
                output = store.block(**args, switch=True)
            else:
                output = getattr(store, command)(**args)
    except (SessionError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
