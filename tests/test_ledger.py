import csv
from contextlib import closing
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ledger.py"
SPEC = importlib.util.spec_from_file_location("linkedin_ledger", SCRIPT)
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(ledger.SCHEMA)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.company = ledger.company(
            self.db, "Fictional Display Ltd", "UK", "https://www.display.example/products",
            "GB-1234", ["Fictional Display"],
        )["company"]
        self.body = self.root / "draft.txt"
        self.body.write_text("How buyers compare display stems for different projects.", encoding="utf-8")

    def add_draft(self, draft_id, kind="first-contact"):
        if kind == "content":
            return ledger.add_draft(
                self.db, draft_id, kind, "Comparing display stems", self.body,
                channel="linkedin", evidence=["Approved specimen photo"],
            )
        return ledger.add_draft(
            self.db, draft_id, kind, "Seasonal arrangement", self.body,
            self.company, "linkedin", "page:fictional-display", ["Fixture catalogue"],
        )

    def approved_draft(self, draft_id, kind="first-contact"):
        result = self.add_draft(draft_id, kind)
        ledger.approve(self.db, draft_id, result["version"], "Reviewer")
        return result["version"]

    def check_history(self, *channels):
        for channel in channels:
            ledger.review_history(self.db, self.company, channel, "checked", "Fixture archive")

    def test_identity_conflicts_are_held_and_aliases_reuse_company(self):
        same = ledger.company(self.db, "Fictional Display", "GB", "display.example")
        self.assertTrue(same["existing"])
        self.assertEqual(same["company"], self.company)
        other = ledger.company(self.db, "Garden Traders", "UK", "garden.example")["company"]
        with self.assertRaises(ledger.LedgerError) as err:
            ledger.company(self.db, "Garden Traders", "GB", "display.example")
        self.assertEqual(err.exception.details["status"], "hold")
        self.assertEqual(set(err.exception.details["candidates"]), {other, self.company})
        self.assertEqual(ledger.report(self.db)["companies"], 2)

    def test_channel_coverage_and_stop_gate_remain_independent_of_drafts(self):
        self.check_history("linkedin")
        check = ledger.gate(self.db, self.company, "linkedin", "first-contact", ["email"])
        self.assertFalse(check["ready"])
        self.assertEqual(check["history_gaps"], ["email"])
        ledger.history(self.db, self.company, "email", "draft", "Draft in archive")
        self.check_history("email")
        self.assertTrue(ledger.gate(self.db, self.company, "linkedin", "first-contact")["ready"])
        ledger.history(self.db, self.company, "email", "contacted", "Mail receipt m01")
        check = ledger.gate(self.db, self.company, "linkedin", "first-contact")
        self.assertIn("already-contacted-across-channels", check["reasons"])
        self.assertTrue(ledger.gate(self.db, self.company, "linkedin", "follow-up")["ready"])
        ledger.history(self.db, self.company, "email", "stopped", "Opt-out fixture")
        check = ledger.gate(self.db, self.company, "linkedin", "follow-up")
        self.assertIn("company-suppressed", check["reasons"])

    def test_unknown_history_needs_explicit_matching_resolution(self):
        self.check_history("linkedin", "email")
        unknown = ledger.history(self.db, self.company, "email", "unknown", "Inconsistent export")["history_id"]
        self.assertIn(
            "unknown-historical-contact",
            ledger.gate(self.db, self.company, "linkedin", "first-contact")["reasons"],
        )
        with self.assertRaises(ledger.LedgerError):
            ledger.history(self.db, self.company, "linkedin", "not-contacted", "Check", resolves=unknown)
        ledger.history(self.db, self.company, "email", "not-contacted", "Original archive confirmed", resolves=unknown)
        self.assertTrue(ledger.gate(self.db, self.company, "linkedin", "first-contact")["ready"])

    def test_modified_draft_needs_fresh_approval(self):
        self.check_history("linkedin")
        old_version = self.approved_draft("contact-1")
        self.body.write_text("An edited fixture.", encoding="utf-8")
        latest = self.add_draft("contact-1")
        self.assertNotEqual(old_version, latest["version"])
        with self.assertRaisesRegex(ledger.LedgerError, "current exact"):
            ledger.approve(self.db, "contact-1", old_version, "Reviewer")
        with self.assertRaisesRegex(ledger.LedgerError, "no recorded approval"):
            ledger.intent(self.db, "contact-1", latest["version"], "attempt-1", "An account")

    def test_unknown_action_is_held_cross_channel_and_receipt_replay_is_idempotent(self):
        self.check_history("linkedin", "email")
        version = self.approved_draft("contact-1")
        intent = ledger.intent(self.db, "contact-1", version, "attempt-1", "An account")
        self.assertEqual(intent["state"], "pending")
        replay = ledger.intent(self.db, "contact-1", version, "attempt-1", "An account")
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["dispatch_permitted"])
        ledger.receipt(self.db, "attempt-1", "unknown", "Interrupted confirmation", "manual")
        self.assertEqual(ledger.report(self.db)["unique_companies_with_contact"], 0)
        with self.assertRaisesRegex(ledger.LedgerError, "unsettled"):
            ledger.intent(self.db, "contact-1", version, "another-key", "An account")
        check = ledger.gate(self.db, self.company, "email", "first-contact")
        self.assertIn("unsettled-action", check["reasons"])
        with self.assertRaisesRegex(ledger.LedgerError, "exact provider receipt"):
            ledger.receipt(self.db, "attempt-1", "confirmed", "Clicked once", "manual")
        args = (self.db, "attempt-1", "confirmed", "Visible original receipt", "manual",
                "msg:example:001", "2026-11-19T10:00:00+08:00")
        self.assertFalse(ledger.receipt(*args)["replayed"])
        self.assertTrue(ledger.receipt(*args)["replayed"])
        self.assertEqual(len(ledger.action_status(self.db, "attempt-1")["receipts"]), 2)
        report = ledger.report(self.db)
        self.assertEqual(report["unique_companies_with_contact"], 1)
        self.assertEqual(report["milestones_by_unique_company"], {})
        ledger.milestone(self.db, self.company, "business-reply", "msg:example:002", "Requests display sample")
        self.assertEqual(ledger.report(self.db)["milestones_by_unique_company"], {"business-reply": 1})

    def test_content_match_and_exact_publication_duplicate(self):
        version = self.approved_draft("post-1", "content")
        ledger.intent(self.db, "post-1", version, "publish-1", "An account")
        ledger.receipt(self.db, "publish-1", "confirmed", "Observed unique post", "manual",
                       "post:001", "2026-11-19T10:00:00+08:00")
        candidate = self.add_draft("post-2", "content")
        self.assertEqual(candidate["similar_content"][0]["id"], "post-1")
        version = candidate["version"]
        ledger.approve(self.db, "post-2", version, "Reviewer")
        with self.assertRaisesRegex(ledger.LedgerError, "same content"):
            ledger.intent(self.db, "post-2", version, "publish-2", "An account")

    def test_linkedin_action_drafts_require_kind_specific_targets_and_evidence(self):
        field = {
            "company": self.company,
            "channel": "linkedin",
            "target": "https://www.linkedin.com/posts/display_activity-4812-abcd/",
            "evidence": ("Public procurement discussion",),
        }
        for kind, overrides, message in (
            ("comment", {"target": "https://www.linkedin.com.example/posts/item"}, "exact HTTPS LinkedIn post"),
            ("comment", {"target": "https://www.linkedin.com/feed/"}, "exact HTTPS LinkedIn post"),
            ("comment", {"channel": "email"}, "requires the linkedin channel"),
            ("comment", {"evidence": ()}, "requires at least one evidence reference"),
            ("group-join", {"target": "https://www.linkedin.com/groups/"}, "exact HTTPS LinkedIn group"),
            ("connection-invite", {"target": "https://www.linkedin.com/company/display/"}, "exact HTTPS LinkedIn personal profile"),
        ):
            with self.subTest(kind=kind, invalid=overrides):
                with self.assertRaisesRegex(ledger.LedgerError, message):
                    ledger.add_draft(self.db, "invalid", kind, "Review", self.body, **(field | overrides))
        self.assertEqual(ledger.report(self.db)["drafts_by_kind"], {})

    def linkedin_draft(self, draft_id, kind, target):
        return ledger.add_draft(
            self.db, draft_id, kind, "Review LinkedIn action", self.body,
            company=self.company, channel="linkedin", target=target, evidence=("Observed fixture page",),
        )

    def test_linkedin_engagement_and_invitations_have_separate_receipts_from_business_contacts(self):
        self.check_history("linkedin")
        for kind, target, external_id in (
            ("comment", "https://www.linkedin.com/posts/display_activity-4812-abcd/", "comment:4812"),
            ("group-join", "https://www.linkedin.com/groups/24180/", "https://www.linkedin.com/groups/24180/"),
            ("connection-invite", "https://www.linkedin.com/in/example-person/", "https://www.linkedin.com/in/example-person/"),
        ):
            with self.subTest(kind=kind):
                version = self.linkedin_draft(kind, kind, target)["version"]
                with self.assertRaisesRegex(ledger.LedgerError, "no recorded approval"):
                    ledger.intent(self.db, kind, version, kind, "An account")
                ledger.approve(self.db, kind, version, "Reviewer")
                pending = ledger.intent(self.db, kind, version, kind, "An account")
                self.assertEqual((pending["kind"], pending["target"], pending["state"]), (kind, target, "pending"))
                self.assertEqual(ledger.status(self.db)["unsettled_actions"][0]["kind"], kind)
                self.assertTrue(ledger.gate(self.db, self.company, "linkedin", "first-contact")["ready"])
                ledger.receipt(
                    self.db, kind, "confirmed", "Observed action after submission", "manual",
                    external_id, "2026-11-19T10:00:00+08:00",
                )
                self.assertIn(
                    "no-established-contact-history", ledger.gate(self.db, self.company, "linkedin", "follow-up")["reasons"],
                )
        report = ledger.report(self.db)
        self.assertEqual(report["confirmed_actions_by_kind"], {"comment": 1, "group-join": 1, "connection-invite": 1})
        self.assertEqual(report["unique_companies_with_contact"], 0)
        self.assertEqual(report["milestones_by_unique_company"], {})

        # The existing contact workflow remains independent, even if a comment and message use the same body.
        version = self.approved_draft("contact-1")
        ledger.intent(self.db, "contact-1", version, "message-1", "An account")
        ledger.receipt(self.db, "message-1", "confirmed", "Message receipt", "manual", "msg:4812", "2026-11-19T11:00:00+08:00")
        self.assertEqual(ledger.report(self.db)["unique_companies_with_contact"], 1)

    def test_comment_duplicate_checks_bind_to_the_post_and_account(self):
        post = "https://www.linkedin.com/posts/display_activity-4812-abcd/"
        equivalent = "https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A4812/?trk=fixture"
        version = self.linkedin_draft("comment-1", "comment", post)["version"]
        ledger.approve(self.db, "comment-1", version, "Reviewer")
        ledger.intent(self.db, "comment-1", version, "comment-key-1", "An account")
        ledger.receipt(self.db, "comment-key-1", "unknown", "Lost confirmation", "manual")
        identical = self.linkedin_draft("comment-2", "comment", equivalent)["version"]
        ledger.approve(self.db, "comment-2", identical, "Reviewer")
        with self.assertRaisesRegex(ledger.LedgerError, "target has a confirmed or unsettled action"):
            ledger.intent(self.db, "comment-2", identical, "comment-key-2", "An account")
        ledger.receipt(
            self.db, "comment-key-1", "confirmed", "Comment visible", "manual",
            "comment:4812", "2026-11-19T10:00:00+08:00",
        )
        with self.assertRaisesRegex(ledger.LedgerError, "same content"):
            ledger.intent(self.db, "comment-2", identical, "comment-key-2", "An account")
        other = self.linkedin_draft("comment-3", "comment", "https://www.linkedin.com/feed/update/urn:li:activity:5262/")["version"]
        ledger.approve(self.db, "comment-3", other, "Reviewer")
        self.assertEqual(ledger.intent(self.db, "comment-3", other, "comment-key-3", "An account")["state"], "pending")

    def test_group_request_duplicate_bound_to_target_and_invitation_respects_suppression(self):
        group = "https://www.linkedin.com/groups/24180/"
        version = self.linkedin_draft("join-1", "group-join", group)["version"]
        ledger.approve(self.db, "join-1", version, "Reviewer")
        ledger.intent(self.db, "join-1", version, "join-key-1", "An account")
        self.body.write_text("Alternative application text", encoding="utf-8")
        version = self.linkedin_draft("join-2", "group-join", group + "?trk=fixture")["version"]
        ledger.approve(self.db, "join-2", version, "Reviewer")
        with self.assertRaisesRegex(ledger.LedgerError, "target has a confirmed or unsettled action"):
            ledger.intent(self.db, "join-2", version, "join-key-2", "An account")
        ledger.receipt(self.db, "join-key-1", "not-executed", "Verified original was not submitted", "manual")
        self.assertEqual(ledger.intent(self.db, "join-2", version, "join-key-2", "An account")["state"], "pending")
        invite = self.linkedin_draft("invite", "connection-invite", "https://www.linkedin.com/in/example-person/")["version"]
        ledger.approve(self.db, "invite", invite, "Reviewer")
        ledger.history(self.db, self.company, "email", "stopped", "Requested no further contact")
        with self.assertRaisesRegex(ledger.LedgerError, "Company is suppressed"):
            ledger.intent(self.db, "invite", invite, "invite-key", "An account")

    def test_latest_metric_snapshot_does_not_add_cumulative_readings(self):
        published = "2026-11-19T09:00:00+08:00"
        ledger.snapshot(self.db, "post-1", published, "2026-11-19T10:00:00+08:00", 3, None, 0)
        ledger.snapshot(self.db, "post-1", published, "2026-11-19T11:00:00+08:00", 15, 4, 0)
        ledger.snapshot(
            self.db, "post-2", published, "2026-11-19T10:00:00+08:00", None, None, 0,
            impressions_state="not-displayed",
        )
        report = ledger.report(self.db)
        self.assertEqual(report["snapshot_count"], 3)
        posts = {row["post"]: row for row in report["latest_post_snapshots"]}
        self.assertEqual(posts["post-1"]["impressions"], 15)
        self.assertEqual(posts["post-1"]["age_seconds"], 7200)
        self.assertIsNone(posts["post-2"]["impressions"])
        self.assertEqual(posts["post-2"]["impressions_state"], "not-displayed")
        self.assertEqual(posts["post-2"]["reach_state"], "unchecked")
        self.assertEqual(posts["post-2"]["interactions"], 0)
        self.assertEqual(posts["post-2"]["interactions_state"], "measured")

    def test_unknown_company_country_holds_contact(self):
        unidentified = ledger.company(self.db, "Unresolved company", "ZZ", "unresolved.example")["company"]
        ledger.review_history(self.db, unidentified, "linkedin", "checked", "Fixture archive")
        result = ledger.gate(self.db, unidentified, "linkedin", "first-contact")
        self.assertEqual(result["reasons"], ["company-country-unverified"])
        ledger.history(self.db, unidentified, "email", "stopped", "Observed opt-out")
        ledger.verify_country(self.db, unidentified, "GB", "Registry record")
        result = ledger.gate(self.db, unidentified, "linkedin", "first-contact")
        self.assertIn("company-suppressed", result["reasons"])
        self.assertNotIn("company-country-unverified", result["reasons"])
        historic = ledger.company(self.db, "Unresolved company", "ZZ", "unresolved.example")
        self.assertEqual((historic["company"], historic["country"]), (unidentified, "GB"))

    def test_country_confirmation_holds_conflict_without_mutating_existing_identity(self):
        unidentified = ledger.company(self.db, "Fictional Display Ltd", "ZZ", "pending.example")["company"]
        with self.assertRaises(ledger.LedgerError) as error:
            ledger.verify_country(self.db, unidentified, "GB", "New registry evidence")
        self.assertEqual(error.exception.details["status"], "hold")
        self.assertEqual(ledger.require_company(self.db, unidentified)["country"], "ZZ")

    def test_csv_bad_row_rolls_back_preceding_rows(self):
        fields = ("name", "country", "domain", "channel", "state", "source")
        source = self.root / "history.csv"
        with source.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(fields)
            w.writerow(("New Lead", "GB", "lead.example", "email", "contacted", "Fixture receipt"))
            w.writerow(("Bad Lead", "", "bad.example", "email", "contacted", "Fixture receipt"))
        db_path = self.root / "ledger.sqlite"
        self.assertEqual(ledger.main(["--db", str(db_path), "import-history", str(source)]), 2)
        with closing(sqlite3.connect(db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
