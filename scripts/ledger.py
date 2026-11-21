"""Local LinkedIn workbench ledger. Commands record actions, never perform them."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

HISTORY_STATES = ("researched", "draft", "contacted", "unknown", "stopped", "rejected", "not-contacted")
COVERAGE_STATES = ("checked", "partial", "unknown")
CONTACT_KINDS = ("first-contact", "follow-up")
LINKEDIN_ACTION_KINDS = ("comment", "group-join", "connection-invite")
DRAFT_KINDS = ("research", "content", *CONTACT_KINDS, *LINKEDIN_ACTION_KINDS)
CONTACT_KIND_PLACEHOLDERS = ",".join("?" for _ in CONTACT_KINDS)
MILESTONES = ("business-reply", "confirmed-demand", "sample", "quote", "order")
ACTION_RESULTS = ("confirmed", "unknown", "not-executed")
METRICS = ("impressions", "reach", "interactions")
METRIC_STATES = ("measured", "not-displayed", "unchecked")

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL,
  registration TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities(
  kind TEXT NOT NULL, key TEXT NOT NULL, company TEXT NOT NULL REFERENCES companies(id),
  PRIMARY KEY(kind, key)
);
CREATE TABLE IF NOT EXISTS country_verifications(
  company TEXT PRIMARY KEY REFERENCES companies(id), country TEXT NOT NULL, source TEXT NOT NULL, verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history(
  id TEXT PRIMARY KEY, company TEXT NOT NULL REFERENCES companies(id),
  channel TEXT NOT NULL, state TEXT NOT NULL, source TEXT NOT NULL,
  occurred_at TEXT, resolves TEXT UNIQUE REFERENCES history(id), recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_reviews(
  id INTEGER PRIMARY KEY, company TEXT NOT NULL REFERENCES companies(id),
  channel TEXT NOT NULL, coverage TEXT NOT NULL, source TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts(
  id TEXT NOT NULL, version TEXT NOT NULL, revision INTEGER NOT NULL,
  kind TEXT NOT NULL, company TEXT REFERENCES companies(id), channel TEXT, target TEXT,
  subject TEXT NOT NULL, body TEXT NOT NULL, review_body TEXT,
  evidence TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(id, version), UNIQUE(id, revision)
);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT NOT NULL, version TEXT NOT NULL, reviewer TEXT NOT NULL, approved_at TEXT NOT NULL,
  PRIMARY KEY(id, version, reviewer),
  FOREIGN KEY(id, version) REFERENCES drafts(id, version)
);
CREATE TABLE IF NOT EXISTS actions(
  key TEXT PRIMARY KEY, draft_id TEXT NOT NULL, version TEXT NOT NULL,
  account TEXT NOT NULL, channel TEXT NOT NULL, company TEXT REFERENCES companies(id),
  created_at TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT, external_id TEXT,
  occurred_at TEXT, source TEXT,
  FOREIGN KEY(draft_id, version) REFERENCES drafts(id, version),
  UNIQUE(channel, account, external_id)
);
CREATE TABLE IF NOT EXISTS receipts(
  id INTEGER PRIMARY KEY, action_key TEXT NOT NULL REFERENCES actions(key),
  result TEXT NOT NULL, evidence TEXT NOT NULL, external_id TEXT, occurred_at TEXT,
  source TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones(
  id TEXT PRIMARY KEY, company TEXT NOT NULL REFERENCES companies(id),
  stage TEXT NOT NULL, evidence TEXT NOT NULL, details TEXT NOT NULL,
  occurred_at TEXT, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
  post TEXT NOT NULL, published_at TEXT NOT NULL, observed_at TEXT NOT NULL,
  age_seconds INTEGER NOT NULL, impressions INTEGER, reach INTEGER, interactions INTEGER,
  impressions_state TEXT NOT NULL DEFAULT 'unchecked',
  reach_state TEXT NOT NULL DEFAULT 'unchecked',
  interactions_state TEXT NOT NULL DEFAULT 'unchecked',
  PRIMARY KEY(post, observed_at)
);
CREATE INDEX IF NOT EXISTS history_by_company ON history(company, state);
CREATE INDEX IF NOT EXISTS reviews_by_company ON history_reviews(company, channel, id);
CREATE INDEX IF NOT EXISTS actions_by_company ON actions(company, state);
CREATE INDEX IF NOT EXISTS actions_by_draft ON actions(draft_id, state);
"""


class LedgerError(Exception):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def value(text, label):
    if not isinstance(text, str) or not text.strip():
        raise LedgerError(f"{label} is required")
    return text.strip()


def canonical(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, re.UNICODE))


def digest(*values):
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def time_format(time):
    return time.astimezone(timezone.utc).isoformat(timespec="microseconds")


def now():
    return time_format(datetime.now(timezone.utc))


def utc(text):
    if text is None or text == "":
        return None
    try:
        time = datetime.fromisoformat(text.removesuffix("Z") + ("+00:00" if text.endswith("Z") else ""))
    except ValueError as error:
        raise LedgerError("Use ISO 8601 with a time zone, for example 2026-11-19T09:00:00+08:00") from error
    if time.utcoffset() is None:
        raise LedgerError("Timestamp needs an explicit time zone")
    return time_format(time)


def channel_name(text):
    channel = value(text, "channel").casefold()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", channel):
        raise LedgerError("Channel must be a stable name such as linkedin or email")
    return channel


def linkedin_target_key(kind, target):
    target = value(target, "LinkedIn target URL")
    try:
        parsed = urlsplit(target)
        if (
            re.search(r"[\s\\\x00-\x1f\x7f]", target)
            or parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)
            or not re.fullmatch(r"(?:[a-z0-9-]+\.)?linkedin\.com", parsed.hostname or "")
        ):
            raise ValueError
        path = unquote(parsed.path).rstrip("/")
        if re.search(r"[\s\\\x00-\x1f\x7f]", path):
            raise ValueError
        if kind == "group-join" and re.fullmatch(r"/groups/[1-9][0-9]*", path):
            return path
        if kind == "connection-invite" and re.fullmatch(r"/in/[^/]+", path):
            return path
        if kind == "comment":
            linked_post = re.fullmatch(r"/feed/update/urn:li:(activity|ugcPost|share):([0-9]+)", path)
            if linked_post:
                return f"post:{linked_post[1]}:{linked_post[2]}"
            if re.fullmatch(r"/posts/[^/]+", path):
                activity = re.search(r"[-_]activity-([0-9]+)(?:-|$)", path)
                return f"post:activity:{activity[1]}" if activity else path
            if re.fullmatch(r"/(?:pulse|groups/[1-9][0-9]*/(?:posts|permalink))/[^/]+", path):
                return path
    except ValueError:
        pass
    expected = {"comment": "post", "group-join": "group", "connection-invite": "personal profile"}
    raise LedgerError(f"{kind} requires an exact HTTPS LinkedIn {expected[kind]} URL")


def country_code(text):
    country = value(text, "country").upper()
    if country == "UK":
        country = "GB"
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise LedgerError("Use a two-letter country code such as GB or US")
    return country


def domain_name(text):
    if not text:
        return None
    text = value(text, "domain")
    try:
        parsed = urlsplit(text if "://" in text else "https://" + text)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            raise ValueError
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").removeprefix("www.")
    except (UnicodeError, ValueError) as error:
        raise LedgerError("Use a public company domain or site URL") from error
    if len(host) > 253 or not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}", host
    ):
        raise LedgerError("Use a public company domain or site URL")
    if host in {"linkedin.com", "facebook.com", "instagram.com", "x.com", "youtube.com"}:
        raise LedgerError("Use the company's own domain. A social URL is evidence rather than a company identity")
    return host


def require_company(db, company_id):
    row = db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise LedgerError("Company ID is not in the ledger", company=company_id)
    return row


def company(db, name, country, domain=None, registration=None, alias=()):
    name = value(name, "name")
    country = country_code(country)
    domain = domain_name(domain)
    number = re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKC", registration).upper()) if registration else None
    if registration and not number:
        raise LedgerError("Registration number must contain letters or digits")
    names = [canonical(value(label, "name or alias")) for label in (name, *alias)]
    if not all(names):
        raise LedgerError("Name and aliases need letters or digits")
    keys = {("name", f"{country}:{label}") for label in names}
    if domain:
        keys.add(("domain", domain))
    if number:
        keys.add(("registration", f"{country}:{number}"))
    matches = {}
    for kind, key in keys:
        found = db.execute("SELECT company FROM identities WHERE kind=? AND key=?", (kind, key)).fetchone()
        if found:
            matches[(kind, key)] = found["company"]
    ids = set(matches.values())
    if len(ids) > 1:
        raise LedgerError("Identity conflict needs review", status="hold", candidates=sorted(ids))
    existing = len(ids) == 1
    company_id = next(iter(ids)) if existing else uuid.uuid4().hex
    if existing:
        current = require_company(db, company_id)
        previous_domains = {
            row["key"] for row in db.execute("SELECT key FROM identities WHERE kind='domain' AND company=?", (company_id,))
        }
        same_registration = bool(number and current["registration"] == number)
        verified_legacy_match = country == "ZZ" and any(kind in ("name", "registration") for kind, _ in matches) and db.execute(
            "SELECT 1 FROM country_verifications WHERE company=?", (company_id,)
        ).fetchone()
        if (
            (current["country"] != country and not verified_legacy_match)
            or number and current["registration"] and not same_registration
            or domain and previous_domains and domain not in previous_domains and not same_registration
        ):
            raise LedgerError("Identity fields disagree and need review", status="hold", candidates=[company_id])
        if number and not current["registration"]:
            db.execute("UPDATE companies SET registration=? WHERE id=?", (number, company_id))
    else:
        db.execute("INSERT INTO companies VALUES (?,?,?,?,?)", (company_id, name, country, number, now()))
    db.executemany(
        "INSERT OR IGNORE INTO identities(kind,key,company) VALUES (?,?,?)",
        [(kind, key, company_id) for kind, key in sorted(keys)],
    )
    return {
        "company": company_id,
        "existing": existing,
        "name": require_company(db, company_id)["name"],
        "country": require_company(db, company_id)["country"],
        "identity_keys": [
            dict(r) for r in db.execute("SELECT kind,key FROM identities WHERE company=? ORDER BY kind,key", (company_id,))
        ],
    }


def verify_country(db, company, country, source):
    identity = require_company(db, company)
    country = country_code(country)
    source = value(source, "country evidence")
    if country == "ZZ":
        raise LedgerError("Supply the verified country code")
    prior = db.execute("SELECT * FROM country_verifications WHERE company=?", (company,)).fetchone()
    if identity["country"] != "ZZ":
        if prior and prior["country"] == country and prior["source"] == source:
            return dict(prior) | {"replayed": True}
        raise LedgerError("This operation resolves an unknown country only", country=identity["country"])
    old_keys = db.execute(
        "SELECT kind,key FROM identities WHERE company=? AND kind IN ('name','registration')",
        (company,),
    )
    new_keys = [(row["kind"], country + ":" + row["key"].split(":", 1)[1]) for row in old_keys]
    conflicts = []
    for kind, key in new_keys:
        match = db.execute("SELECT company FROM identities WHERE kind=? AND key=?", (kind,key)).fetchone()
        if match and match["company"] != company:
            conflicts.append(match["company"])
    if conflicts:
        raise LedgerError("Verified country creates an identity conflict", status="hold", candidates=sorted(set(conflicts)))
    at = now()
    db.executemany("INSERT OR IGNORE INTO identities VALUES(?,?,?)", [(kind,key,company) for kind,key in new_keys])
    db.execute("UPDATE companies SET country=? WHERE id=?", (country,company))
    db.execute("INSERT INTO country_verifications VALUES(?,?,?,?)", (company,country,source,at))
    return {"company": company, "country": country, "source": source, "verified_at": at, "replayed": False}


def history(db, company, channel, state, source, occurred_at=None, resolves=None):
    require_company(db, company)
    channel = channel_name(channel)
    source = value(source, "source")
    occurred = utc(occurred_at)
    if state not in HISTORY_STATES:
        raise LedgerError("Unknown history state", states=HISTORY_STATES)
    if resolves and state not in ("contacted", "not-contacted"):
        raise LedgerError("Resolve an unknown with contacted or not-contacted")
    if resolves:
        prior = db.execute("SELECT company, channel, state FROM history WHERE id=?", (resolves,)).fetchone()
        if prior is None or tuple(prior) != (company, channel, "unknown"):
            raise LedgerError("Resolution must refer to unknown history for the same company and channel")
    event_id = digest(company, channel, state, source, occurred, resolves)
    found = db.execute("SELECT id FROM history WHERE id=?", (event_id,)).fetchone()
    if not found:
        db.execute(
            "INSERT INTO history VALUES(?,?,?,?,?,?,?,?)",
            (event_id, company, channel, state, source, occurred, resolves, now()),
        )
    return {"history_id": event_id, "existing": bool(found), "company": company, "channel": channel, "state": state}


def review_history(db, company, channel, coverage, source):
    require_company(db, company)
    channel = channel_name(channel)
    if coverage not in COVERAGE_STATES:
        raise LedgerError("Unknown history coverage", coverages=COVERAGE_STATES)
    created = now()
    result = db.execute(
        "INSERT INTO history_reviews(company,channel,coverage,source,recorded_at) VALUES(?,?,?,?,?)",
        (company, channel, coverage, value(source, "source"), created),
    )
    return {"review_id": result.lastrowid, "company": company, "channel": channel, "coverage": coverage}


def import_history(db, path):
    count = 0
    new = 0
    reviews = 0
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        rows = csv.DictReader(file)
        required = {"channel", "state", "source"}
        fields = set(rows.fieldnames or ())
        if not required.issubset(fields) or not ("company" in fields or {"name", "country"} <= fields):
            raise LedgerError("CSV needs channel,state,source and either company or name,country")
        for row in rows:
            try:
                company_id = (row.get("company") or "").strip()
                if not company_id:
                    identity = company(
                        db, row.get("name"), row.get("country"), row.get("domain"), row.get("registration"),
                        [x.strip() for x in (row.get("aliases") or "").split("|") if x.strip()],
                    )
                    company_id = identity["company"]
                result = history(
                    db, company_id, row.get("channel"), row.get("state"),
                    row.get("source"), row.get("occurred_at"), row.get("resolves") or None,
                )
                new += not result["existing"]
                count += 1
                if row.get("coverage"):
                    current = db.execute(
                        "SELECT coverage,source FROM history_reviews WHERE company=? AND channel=? ORDER BY id DESC LIMIT 1",
                        (company_id, result["channel"]),
                    ).fetchone()
                    if current is None or tuple(current) != (row["coverage"], row["source"].strip()):
                        review_history(db, company_id, result["channel"], row["coverage"], row["source"])
                        reviews += 1
            except (LedgerError, sqlite3.IntegrityError) as error:
                details = error.details if isinstance(error, LedgerError) else {}
                raise LedgerError(f"History CSV line {rows.line_num}: {error}", **details) from error
    return {"rows": count, "inserted_history": new, "inserted_reviews": reviews}


def gate(db, company, channel, kind, required_channels=()):
    identity = require_company(db, company)
    channel = channel_name(channel)
    channels = {channel, *(channel_name(x) for x in required_channels)}
    channels.update(r[0] for r in db.execute("SELECT DISTINCT channel FROM history WHERE company=?", (company,)))
    contact_actions = db.execute(
        "SELECT a.key,a.channel,a.state FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
        f"WHERE a.company=? AND d.kind IN ({CONTACT_KIND_PLACEHOLDERS}) ORDER BY a.created_at",
        (company, *CONTACT_KINDS),
    ).fetchall()
    channels.update(row["channel"] for row in contact_actions)
    all_reviews = db.execute(
        "SELECT channel,coverage,source,recorded_at FROM history_reviews WHERE company=? ORDER BY id", (company,)
    )
    coverage = {r["channel"]: dict(r) for r in all_reviews}
    channels.update(coverage)
    reasons = []
    if identity["country"] == "ZZ":
        reasons.append("company-country-unverified")
    if db.execute("SELECT 1 FROM history WHERE company=? AND state IN ('stopped','rejected')", (company,)).fetchone():
        reasons.append("company-suppressed")
    unsettled_history = db.execute(
        "SELECT id FROM history h WHERE company=? AND state='unknown' AND NOT EXISTS "
        "(SELECT 1 FROM history r WHERE r.resolves=h.id)", (company,)
    ).fetchall()
    if unsettled_history:
        reasons.append("unknown-historical-contact")
    pending = [row for row in contact_actions if row["state"] in ("pending", "unknown")]
    if pending:
        reasons.append("unsettled-action")
    prior = bool(
        db.execute("SELECT 1 FROM history WHERE company=? AND state='contacted'", (company,)).fetchone()
        or any(row["state"] == "confirmed" for row in contact_actions)
    )
    if prior and kind == "first-contact":
        reasons.append("already-contacted-across-channels")
    if not prior and kind == "follow-up":
        reasons.append("no-established-contact-history")
    gaps = sorted(ch for ch in channels if coverage.get(ch, {}).get("coverage") != "checked")
    if gaps:
        reasons.append("history-coverage-incomplete")
    return {
        "ready": not reasons, "reasons": reasons, "prior_contact": prior, "required_channels": sorted(channels),
        "history_gaps": gaps, "coverage": coverage, "pending_actions": [r["key"] for r in pending],
        "unknown_history": [r["id"] for r in unsettled_history],
    }


def latest_draft(db, draft_id):
    row = db.execute("SELECT * FROM drafts WHERE id=? ORDER BY revision DESC LIMIT 1", (draft_id,)).fetchone()
    if row is None:
        raise LedgerError("Draft not found", id=draft_id)
    return row


def similarity(a, b):
    a, b = canonical(a), canonical(b)
    if not a or not b:
        return 0
    if a == b:
        return 1
    a = {a[i:i + 3] for i in range(len(a) - 2)}
    b = {b[i:i + 3] for i in range(len(b) - 2)}
    return len(a & b) / len(a | b) if a and b else 0


def content_matches(db, draft):
    result = {}
    for other in db.execute(
        "SELECT d.id,d.version,d.subject,d.body FROM drafts d WHERE d.kind='content' AND d.id<>? AND ("
        "d.revision=(SELECT MAX(r.revision) FROM drafts r WHERE r.id=d.id) OR EXISTS "
        "(SELECT 1 FROM actions a WHERE a.draft_id=d.id AND a.version=d.version AND a.state='confirmed'))",
        (draft["id"],),
    ):
        title_score = similarity(draft["subject"], other["subject"])
        body_score = similarity(draft["body"], other["body"])
        if max(title_score, body_score) >= 0.65:
            old = result.get(other["id"])
            if old is None or max(title_score, body_score) > max(old["subject_similarity"], old["body_similarity"]):
                result[other["id"]] = {
                    "id": other["id"], "version": other["version"], "subject": other["subject"],
                    "subject_similarity": round(title_score, 3), "body_similarity": round(body_score, 3),
                }
    return sorted(result.values(), key=lambda item: -max(item["body_similarity"], item["subject_similarity"]))[:12]


def add_draft(db, id, kind, subject, body_file, company=None, channel=None, target=None, evidence=(), review_file=None):
    if kind not in DRAFT_KINDS:
        raise LedgerError("Unknown draft kind")
    draft_id = value(id, "draft ID")
    subject = value(subject, "subject")
    body = Path(body_file).read_text(encoding="utf-8-sig")
    value(body, "body")
    review = Path(review_file).read_text(encoding="utf-8-sig") if review_file else None
    refs = sorted({value(item, "evidence") for item in evidence})
    if company:
        require_company(db, company)
    if kind in CONTACT_KINDS and (not company or not target or not refs):
        raise LedgerError("A contact draft requires company ID, precise target and at least one evidence reference")
    if kind == "content":
        if company:
            raise LedgerError("Content drafts are distinct from company contact drafts")
        if not refs:
            raise LedgerError("A content draft requires a source or material reference")
    if kind != "research" and not channel:
        raise LedgerError("External-action drafts require the intended channel")
    if channel:
        channel = channel_name(channel)
    if target:
        target = value(target, "target")
    if kind in LINKEDIN_ACTION_KINDS:
        if channel != "linkedin":
            raise LedgerError(f"{kind} requires the linkedin channel")
        if not refs:
            raise LedgerError(f"{kind} requires at least one evidence reference")
        linkedin_target_key(kind, target)
    prior = db.execute("SELECT * FROM drafts WHERE id=? ORDER BY revision DESC LIMIT 1", (draft_id,)).fetchone()
    identity = (kind, company, channel, target)
    if prior and identity != (prior["kind"], prior["company"], prior["channel"], prior["target"]):
        raise LedgerError("A draft ID has a fixed kind, company, channel and target. Use a new ID for a different action")
    if prior and (prior["subject"], prior["body"], prior["review_body"], prior["evidence"]) == (
        subject, body, review, json.dumps(refs, ensure_ascii=False)
    ):
        return draft_status(db, draft_id)
    revision = (prior["revision"] + 1) if prior else 1
    version = digest(draft_id, revision, identity, subject, body, review, refs)
    db.execute(
        "INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (draft_id, version, revision, kind, company, channel, target, subject, body, review, json.dumps(refs, ensure_ascii=False), now()),
    )
    return draft_status(db, draft_id)


def draft_status(db, id, show_body=False):
    row = dict(latest_draft(db, id))
    approvals = db.execute("SELECT reviewer,approved_at FROM approvals WHERE id=? AND version=? ORDER BY approved_at", (id, row["version"]))
    row["approvals"] = [dict(a) for a in approvals]
    row["evidence"] = json.loads(row["evidence"])
    if row["kind"] == "content":
        row["similar_content"] = content_matches(db, row)
    if not show_body:
        del row["body"]
        del row["review_body"]
    return row


def approve(db, id, version, by):
    row = latest_draft(db, id)
    if row["version"] != version:
        raise LedgerError("Review applies only to the current exact draft revision")
    if row["kind"] == "research":
        raise LedgerError("Research records do not need approval for external action")
    db.execute(
        "INSERT OR IGNORE INTO approvals VALUES(?,?,?,?)",
        (id, version, value(by, "reviewer"), now()),
    )
    return draft_status(db, id)


def get_action(db, key):
    row = db.execute(
        "SELECT a.*,d.kind,d.target FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
        "WHERE a.key=?", (key,),
    ).fetchone()
    if not row:
        raise LedgerError("Action key not found", key=key)
    return dict(row)


def intent(db, id, version, key, account, required_channels=()):
    key = value(key, "stable action key")
    account = canonical(value(account, "account"))
    if not account:
        raise LedgerError("Account ID needs letters or digits")
    known = db.execute("SELECT * FROM actions WHERE key=?", (key,)).fetchone()
    if known:
        if (known["draft_id"], known["version"], known["account"]) != (id, version, account):
            raise LedgerError("Action key already belongs to another revision, draft or account")
        return get_action(db, key) | {"replayed": True, "local_record_only": True, "dispatch_permitted": False}
    draft = latest_draft(db, id)
    if draft["version"] != version:
        raise LedgerError("Current exact draft revision is required")
    if draft["kind"] == "research":
        raise LedgerError("Research records have no external action")
    if not db.execute("SELECT 1 FROM approvals WHERE id=? AND version=?", (id,version)).fetchone():
        raise LedgerError("This draft version has no recorded approval")
    if db.execute("SELECT 1 FROM actions WHERE draft_id=? AND state<>'not-executed'", (id,)).fetchone():
        raise LedgerError("This draft has a confirmed or unsettled action. Read or reconcile the existing action")
    if draft["kind"] in CONTACT_KINDS:
        check = gate(db, draft["company"], draft["channel"], draft["kind"], required_channels)
        if not check["ready"]:
            raise LedgerError("Contact history check needs attention", **check)
        prior_actions = db.execute(
            "SELECT a.key,d.body FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
            f"WHERE a.company=? AND d.kind IN ({CONTACT_KIND_PLACEHOLDERS}) AND a.state<>'not-executed'",
            (draft["company"], *CONTACT_KINDS),
        )
    elif draft["kind"] in LINKEDIN_ACTION_KINDS:
        kind = draft["kind"]
        if kind == "connection-invite" and draft["company"] and db.execute(
            "SELECT 1 FROM history WHERE company=? AND state IN ('stopped','rejected')", (draft["company"],)
        ).fetchone():
            raise LedgerError("Company is suppressed")
        target_key = linkedin_target_key(kind, draft["target"])
        earlier = db.execute(
            "SELECT a.key,a.state,d.target,d.body FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
            "WHERE a.channel='linkedin' AND a.account=? AND d.kind=? AND a.state<>'not-executed'",
            (account, kind),
        )
        prior_actions = []
        for previous in earlier:
            if linkedin_target_key(kind, previous["target"]) != target_key:
                continue
            if kind != "comment" or previous["state"] != "confirmed":
                raise LedgerError("This LinkedIn target has a confirmed or unsettled action", existing_key=previous["key"])
            prior_actions.append(previous)
    else:
        prior_actions = db.execute(
            "SELECT a.key,d.body FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
            "WHERE a.channel=? AND a.account=? AND d.kind='content' AND a.state<>'not-executed'",
            (draft["channel"], account),
        )
    body = canonical(draft["body"])
    for item in prior_actions:
        if canonical(item["body"]) == body:
            raise LedgerError("The same content already has a confirmed or unsettled action", existing_key=item["key"])
    db.execute(
        "INSERT INTO actions(key,draft_id,version,account,channel,company,created_at,state) VALUES(?,?,?,?,?,?,?,?)",
        (key,id,version,account,draft["channel"],draft["company"],now(),"pending"),
    )
    return get_action(db,key) | {"replayed": False, "local_record_only": True}


def receipt(db, key, result, evidence, source, external_id=None, occurred_at=None):
    action = get_action(db, key)
    evidence = value(evidence, "evidence")
    if external_id is not None:
        external_id = value(external_id, "external ID")
    observed = utc(occurred_at)
    if result == "confirmed" and (not external_id or not observed):
        raise LedgerError("A confirmed action needs an exact provider receipt ID or URL and a time with a time zone")
    if result != "confirmed" and external_id:
        raise LedgerError("Supply an external receipt ID only when the action is confirmed")
    prior_record = db.execute(
        "SELECT result,evidence,external_id,occurred_at,source FROM receipts WHERE action_key=? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    if prior_record and tuple(prior_record) == (result,evidence,external_id,observed,source):
        return action | {"replayed": True}
    if action["state"] not in ("pending","unknown"):
        raise LedgerError("Action has a final recorded result", state=action["state"])
    db.execute(
        "UPDATE actions SET state=?,evidence=?,external_id=?,occurred_at=?,source=? WHERE key=?",
        (result,evidence,external_id,observed,source,key),
    )
    db.execute(
        "INSERT INTO receipts(action_key,result,evidence,external_id,occurred_at,source,recorded_at) VALUES(?,?,?,?,?,?,?)",
        (key,result,evidence,external_id,observed,source,now()),
    )
    return get_action(db,key) | {"replayed": False}


def milestone(db, company, stage, evidence, details, occurred_at=None):
    require_company(db, company)
    evidence = value(evidence, "evidence")
    details = value(details, "business details")
    occurred = utc(occurred_at)
    event_id = digest(company, stage, evidence, details, occurred)
    result = db.execute(
        "INSERT OR IGNORE INTO milestones VALUES(?,?,?,?,?,?,?)",
        (event_id,company,stage,evidence,details,occurred,now()),
    )
    return {"milestone_id": event_id, "company": company, "stage": stage, "replayed": result.rowcount == 0}


def snapshot(
    db, post, published_at, observed_at, impressions=None, reach=None, interactions=None,
    impressions_state=None, reach_state=None, interactions_state=None,
):
    post = value(post, "post")
    published = utc(value(published_at, "published time"))
    observed = utc(value(observed_at, "observed time"))
    seconds = int((datetime.fromisoformat(observed) - datetime.fromisoformat(published)).total_seconds())
    if observed < published or any(count is not None and count < 0 for count in (impressions, reach, interactions)):
        raise LedgerError("Observation must follow publication and metrics must be nonnegative")
    earlier = db.execute("SELECT published_at FROM snapshots WHERE post=? LIMIT 1", (post,)).fetchone()
    if earlier and earlier["published_at"] != published:
        raise LedgerError("A post ID must have a consistent publication time")
    existing = db.execute("SELECT * FROM snapshots WHERE post=? AND observed_at=?", (post,observed)).fetchone()
    row = dict(post=post, published_at=published, observed_at=observed, age_seconds=seconds)
    for metric, number, state in zip(
        METRICS, (impressions, reach, interactions), (impressions_state, reach_state, interactions_state)
    ):
        state = state or ("measured" if number is not None else "unchecked")
        if state not in METRIC_STATES or (number is not None) != (state == "measured"):
            raise LedgerError(f"Inconsistent value and observation state for {metric}")
        row[metric] = number
        row[metric + "_state"] = state
    if existing and dict(existing) != row:
        raise LedgerError("This exact snapshot already exists with different measurements")
    if not existing:
        db.execute(
            "INSERT INTO snapshots(post,published_at,observed_at,age_seconds,impressions,impressions_state,"
            "reach,reach_state,interactions,interactions_state) VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()),
        )
    return row | {"replayed": bool(existing)}


def counts(db, query):
    return dict(db.execute(query).fetchall())


def report(db):
    return {
        "companies": db.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "drafts_by_kind": counts(db, "SELECT kind,COUNT(DISTINCT id) FROM drafts GROUP BY kind"),
        "actions_by_result": counts(db, "SELECT state, COUNT(*) FROM actions GROUP BY state"),
        "confirmed_actions_by_kind": counts(
            db, "SELECT d.kind, COUNT(*) FROM actions a JOIN drafts d ON a.draft_id=d.id AND a.version=d.version "
            "WHERE a.state='confirmed' GROUP BY d.kind",
        ),
        "unique_companies_with_contact": db.execute(
            "SELECT COUNT(*) FROM (SELECT company FROM history WHERE state='contacted' UNION "
            "SELECT a.company FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
            f"WHERE a.state='confirmed' AND a.company IS NOT NULL AND d.kind IN ({CONTACT_KIND_PLACEHOLDERS}))",
            CONTACT_KINDS,
        ).fetchone()[0],
        "milestones_by_unique_company": counts(db, "SELECT stage,COUNT(DISTINCT company) FROM milestones GROUP BY stage"),
        "snapshot_count": db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
        "latest_post_snapshots": [dict(r) for r in db.execute(
            "SELECT s.* FROM snapshots s WHERE NOT EXISTS (SELECT 1 FROM snapshots newer WHERE newer.post=s.post "
            "AND newer.observed_at>s.observed_at) ORDER BY observed_at DESC,post"
        )],
    }


def status(db, company=None):
    if company:
        return {
            "company": dict(require_company(db, company)),
            "identity_keys": [dict(r) for r in db.execute(
                "SELECT kind,key FROM identities WHERE company=? ORDER BY kind,key", (company,)
            )],
            "country_verifications": [dict(r) for r in db.execute(
                "SELECT * FROM country_verifications WHERE company=?", (company,)
            )],
            "history": [dict(r) for r in db.execute("SELECT * FROM history WHERE company=? ORDER BY recorded_at", (company,))],
            "reviews": [dict(r) for r in db.execute("SELECT * FROM history_reviews WHERE company=? ORDER BY id", (company,))],
            "actions": [dict(r) for r in db.execute(
                "SELECT a.*,d.kind,d.target FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
                "WHERE a.company=? ORDER BY a.created_at", (company,),
            )],
            "milestones": [dict(r) for r in db.execute("SELECT * FROM milestones WHERE company=? ORDER BY recorded_at", (company,))],
        }
    return report(db) | {"unsettled_actions": [dict(r) for r in db.execute(
        "SELECT a.*,d.kind,d.target FROM actions a JOIN drafts d ON d.id=a.draft_id AND d.version=a.version "
        "WHERE a.state IN ('pending','unknown') ORDER BY a.created_at"
    )]}


def action_status(db, key):
    return get_action(db, key) | {
        "receipts": [dict(r) for r in db.execute("SELECT * FROM receipts WHERE action_key=? ORDER BY id", (key,))]
    }


def snapshots(db, post):
    return [dict(r) for r in db.execute("SELECT * FROM snapshots WHERE post=? ORDER BY observed_at", (post,))]


def upgrade_snapshots(db):
    columns = {row["name"] for row in db.execute("PRAGMA table_info(snapshots)")}
    for metric in METRICS:
        state = metric + "_state"
        if state not in columns:
            db.execute(f"ALTER TABLE snapshots ADD COLUMN {state} TEXT NOT NULL DEFAULT 'unchecked'")
            db.execute(f"UPDATE snapshots SET {state}='measured' WHERE {metric} IS NOT NULL")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", type=Path, required=True, help="Local SQLite file")
    commands = root.add_subparsers(dest="command", required=True)

    def command(name, handler, help):
        args = commands.add_parser(name, help=help)
        args.set_defaults(handler=handler)
        return args

    def fields(args, *names):
        for name in names:
            args.add_argument("--" + name, required=True)

    args = command("company", company, "identify a company by name, registration, domain and aliases")
    fields(args, "name", "country")
    args.add_argument("--domain")
    args.add_argument("--registration")
    args.add_argument("--alias", action="append", default=[])

    args = command("verify-country", verify_country, "resolve an unknown country with evidence and preserve the company ID")
    fields(args, "company", "country", "source")

    args = command("history", history, "record a cross-channel contact-history fact")
    fields(args, "company", "channel", "source")
    args.add_argument("--state", choices=HISTORY_STATES, required=True)
    args.add_argument("--occurred-at")
    args.add_argument("--resolves", help="ID of an unknown history entry explicitly resolved by this record")

    args = command("history-review", review_history, "record the precise scope and result of a history check")
    fields(args, "company", "channel", "source")
    args.add_argument("--coverage", choices=COVERAGE_STATES, required=True)

    args = command("import-history", import_history, "import a UTF-8 CSV as a single SQLite transaction")
    args.add_argument("path", type=Path)

    args = command("gate", gate, "check first contact or follow-up readiness against known history")
    fields(args, "company", "channel")
    args.add_argument("--kind", choices=CONTACT_KINDS, required=True)
    args.add_argument("--required-channel", dest="required_channels", action="append", default=[])

    args = command("draft", add_draft, "save an exact immutable draft revision")
    fields(args, "id", "subject", "body-file")
    args.add_argument("--kind", choices=DRAFT_KINDS, required=True)
    args.add_argument("--review-file", help="Optional Chinese review copy in UTF-8")
    args.add_argument("--company")
    args.add_argument("--channel")
    args.add_argument("--target")
    args.add_argument("--evidence", action="append", default=[])

    args = command("show-draft", draft_status, "display current revision, evidence, review and duplicate hints")
    fields(args, "id")
    args.add_argument("--show-body", action="store_true")

    args = command("approve", approve, "record a reviewed exact revision")
    fields(args, "id", "version", "by")

    args = command("intent", intent, "reserve a unique manual or authorized-connector action in the local ledger")
    fields(args, "id", "version", "key", "account")
    args.add_argument("--required-channel", dest="required_channels", action="append", default=[])

    args = command("receipt", receipt, "record or reconcile the result of an independently performed action")
    fields(args, "key", "evidence")
    args.add_argument("--result", required=True, choices=ACTION_RESULTS)
    args.add_argument("--source", required=True, choices=("manual", "authorized-connector"))
    args.add_argument("--external-id", help="Exact message, post or action ID/URL rather than a conversation URL")
    args.add_argument("--occurred-at", help="Timestamp with zone required when confirmed")

    args = command("action", action_status, "inspect one reservation and all result records")
    fields(args, "key")

    args = command("milestone", milestone, "record a verified business response, demand or opportunity event")
    fields(args, "company", "evidence", "details")
    args.add_argument("--stage", required=True, choices=MILESTONES)
    args.add_argument("--occurred-at")

    args = command("snapshot", snapshot, "save one cumulative post reading with its observation age")
    fields(args, "post", "published-at", "observed-at")
    for name in METRICS:
        args.add_argument("--" + name, type=int)
        args.add_argument("--" + name + "-state", choices=METRIC_STATES,
                          help=f"Observation state for {name}. Default: measured if supplied, otherwise unchecked")

    args = command("snapshots", snapshots, "list all readings for one post")
    fields(args, "post")

    args = command("status", status, "inspect one company or the overall ledger")
    args.add_argument("--company")
    command("report", report, "summarize unique companies, action outcomes and latest post snapshots")
    return root


def main(argv=None):
    args = vars(parser().parse_args(argv))
    db_path = args.pop("db").expanduser()
    handler = args.pop("handler")
    del args["command"]
    db = None
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        with db:
            upgrade_snapshots(db)
            result = handler(db, **args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LedgerError, sqlite3.Error, OSError, ValueError, csv.Error) as error:
        details = error.details if isinstance(error, LedgerError) else {}
        print(json.dumps({"error": str(error)} | details,ensure_ascii=False,indent=2), file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
