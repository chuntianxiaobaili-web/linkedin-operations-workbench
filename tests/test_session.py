from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from session import SessionError, SessionStore, duration_seconds, parse_timestamp


class SessionTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory()) if hasattr(self, "enterContext") else None
        if folder is None:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            folder = temp.name
        self.db = Path(folder) / "session.sqlite"
        self.now = 1_800_000_000.0
        self.store = self.open_store()
        self.addCleanup(self.store.connection.close)

    def open_store(self):
        return SessionStore(self.db, lambda: self.now)

    def start(self, duration=None):
        return self.store.start("每家公司一份研究", duration=duration, scope="英国仿真花")["run_id"]

    def add(self, run, title, **kwargs):
        return self.store.add(run, kind=kwargs.pop("kind", "research"), title=title, **kwargs)["task"]["id"]

    def test_restarts_keep_cutoff_and_after_deadline_only_finish_active_task(self):
        run = self.start(duration_seconds("1分钟"))
        research = self.add(run, "Verify company")
        queued = self.add(run, "Draft original post", kind="content", priority=10)
        deadline = self.store.claim(run)["deadline"]
        self.now += 61
        with self.open_store() as restarted:
            state = restarted.claim(run)
            self.assertEqual(state["deadline"], deadline)
            self.assertEqual(state["status"], "expired")
            self.assertEqual(state["task"]["id"], research)
            restarted.done(run, research, result="local-company-note.md", worked=18)
            summary = restarted.close(run)
        self.assertEqual(summary["status"], "closed")
        self.assertEqual(summary["window_elapsed_seconds"], 60)
        self.assertEqual(summary["after_deadline_seconds"], 1)
        self.assertEqual(summary["counts"]["done"], 1)
        self.assertEqual(summary["counts"]["queued"], 1)
        self.assertEqual(summary["time_seconds"]["active_work"], 18)
        self.assertEqual(summary["time_seconds"]["unmeasured_work"], 43)
        self.assertEqual(summary["tasks"][1]["id"], queued)

    def test_resume_preserves_deadline_and_requires_reconciling_outcome(self):
        run = self.start(100)
        first = self.add(run, "Record manually submitted post", kind="receipt")
        followup = self.add(run, "Update local notes", kind="knowledge")
        initial = self.store.claim(run)
        self.now += 12
        with self.open_store() as restarted:
            pick = restarted.resume(run, reason="Session interrupted")
            self.assertEqual(pick["task"]["id"], followup)
            self.assertEqual(pick["deadline"], initial["deadline"])
            self.assertEqual(restarted.report(run)["time_seconds"]["interrupted"], 12)
            with self.assertRaisesRegex(SessionError, "Check the old outcome"):
                restarted.requeue(run, first, reason="restart")
            restarted.done(run, first, result="Local receipt R-1")
            self.assertEqual(restarted.next(run)["task"]["id"], followup)

    def test_switch_moves_to_useful_local_work_and_keeps_measured_time(self):
        run = self.start()
        receipt = self.add(run, "Verify manual receipt", kind="receipt", priority=90)
        content = self.add(run, "Edit checklist", kind="content", priority=60)
        self.assertEqual(self.store.claim(run)["task"]["id"], receipt)
        self.now += 20
        swapped = self.store.block(run, receipt, reason="等待用户提供回执", waiting=True, switch=True, worked=4)
        self.assertEqual(swapped["task"]["status"], "waiting")
        self.assertEqual(swapped["next"]["task"]["id"], content)
        self.now += 30
        self.store.done(run, content, result="checklist-draft.md", worked=20)
        self.now += 90
        report = self.store.report(run)
        self.assertIsNone(report["deadline"])
        self.assertEqual(report["status"], "waiting")
        self.assertEqual(report["time_seconds"]["active_work"], 24)
        self.assertEqual(report["time_seconds"]["unmeasured_work"], 26)
        self.assertEqual(report["time_seconds"]["waiting"], 90)
        self.store.requeue(run, receipt, reason="User returned receipt")
        self.assertEqual(self.store.claim(run)["task"]["id"], receipt)

    def test_waits_stop_accruing_when_scheduled_task_becomes_ready(self):
        run = self.start(75)
        task = self.add(run, "Check permitted export", due=self.now + 30)
        self.assertEqual(self.store.claim(run)["status"], "waiting")
        self.now += 50
        report = self.store.report(run)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["time_seconds"]["waiting"], 30)
        self.assertEqual(report["time_seconds"]["idle"], 20)
        self.store.block(run, task, reason="Export unavailable")
        self.now += 50
        report = self.store.report(run)
        self.assertEqual(report["status"], "expired")
        self.assertEqual(report["time_seconds"]["blocked"], 25)
        self.assertEqual(report["time_seconds"]["idle"], 45)
        self.assertEqual(sum(report["time_seconds"].values()), report["elapsed_seconds"])

    def test_exact_deadline_disallows_claim_and_bad_work_count_rolls_back(self):
        run = self.start(10)
        task = self.add(run, "Local research")
        self.store.claim(run)
        with self.assertRaisesRegex(SessionError, "Measured work"):
            self.store.done(run, task, result="result.md", worked=11)
        self.assertEqual(self.store.report(run)["counts"]["running"], 1)
        with self.assertRaisesRegex(SessionError, "Account for the running task"):
            self.store.close(run)
        self.now += 10
        self.store.done(run, task, result="result.md")
        self.assertEqual(self.store.claim(run)["status"], "expired")
        self.assertEqual(self.store.report(run)["after_deadline_seconds"], 0)

    def test_duplicate_tasks_and_completed_retries_are_idempotent(self):
        run = self.start()
        task = self.add(run, "Company A", key="account:A")
        self.assertEqual(self.add(run, "Company A", key="account:A"), task)
        with self.assertRaisesRegex(SessionError, "bound to different"):
            self.add(run, "Company B", key="account:A")
        self.store.claim(run)
        self.now += 5
        result = self.store.done(run, task, result="File A", worked=3)
        self.assertEqual(self.store.done(run, task, result="File A", worked=3), result)
        report = self.store.report(run, events=True)
        self.assertEqual(report["time_seconds"]["active_work"], 3)
        self.assertEqual([event["event"] for event in report["events"]].count("done"), 1)

    def test_scope_and_cli_result(self):
        import json

        tool = Path(__file__).resolve().parents[1] / "scripts" / "session.py"
        args = [sys.executable, "-B", str(tool), "--db", str(self.db)]
        result = subprocess.check_output(
            [*args, "start", "--goal", "研究花艺公司", "--scope", "英国", "--duration", "2小时30分钟"],
            text=True,
        )
        run = json.loads(result)
        self.assertEqual(run["goal"], "研究花艺公司")
        self.assertEqual(json.loads(subprocess.check_output([*args, "next", run["run_id"]], text=True))["status"], "empty")
        self.assertEqual(parse_timestamp(run["deadline"]) - parse_timestamp(run["started_at"]), 9000)


if __name__ == "__main__":
    unittest.main()
