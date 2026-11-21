"""Convert a ProspectDesk-style deduplication master to the ledger CSV format."""
import argparse
import csv
import json
from pathlib import Path
import re

COUNTRIES = {
    "germany": "DE", "united kingdom": "GB", "australia": "AU", "united states": "US",
    "new zealand": "NZ", "italy": "IT", "cyprus": "CY", "serbia": "RS",
    "netherlands": "NL", "canada": "CA",
}
FIELDS = ("name", "country", "domain", "registration", "aliases", "channel", "state", "source")
STOP = {"SUPPRESSED", "DO_NOT_CONTACT", "OPTED_OUT", "UNSUBSCRIBED", "REJECTED"}
CONTACT = {"DELIVERED", "DELIVERY_FAILED", "HISTORICAL_EMAIL_FOUND_NO_NEW_OUTREACH",
           "REPLIED_NEUTRAL", "REPLIED_POSITIVE", "DELIVERED_TO_RECIPIENT_SERVER"}
RESEARCH_HOLD = {"HOLD_ENTITY_UNVERIFIED", "HOLD_REGISTRY_UNVERIFIED", "DUPLICATE_EXCLUDED"}


def country_code(value):
    key = str(value or "").strip().casefold()
    if key == "uk":
        return "GB"
    return COUNTRIES.get(key) or (key.upper() if re.fullmatch("[a-z]{2}", key) else "ZZ")


def state_for(status):
    if status in STOP:
        return "stopped" if status != "REJECTED" else "rejected"
    if "RETRY" in status or status.startswith("HOLD") and status not in RESEARCH_HOLD or "OUTREACH_HOLD" in status:
        return "unknown"
    if status in CONTACT or status.startswith("SENT_") or status in {
        "LINKEDIN_COMPANY_MESSAGE_SENT_NO_REPLY", "LINKEDIN_SENT_AWAITING_REPLY",
        "LINKEDIN_INVITATION_SENT_PENDING",
    }:
        return "contacted"
    if status.startswith("DRAFT") or status == "CONFIRMATION_REQUIRED":
        return "draft"
    if status.startswith("RESEARCH") or status in RESEARCH_HOLD:
        return "researched"
    return "unknown"


def channel_for(record, status):
    text = " ".join(str(record.get(k, "") or "") for k in ("outboundChannel", "channel")).casefold()
    if status.startswith("LINKEDIN"):
        return "linkedin"
    if "supplier" in text and "form" in text:
        return "supplier-form"
    if "whatsapp" in text:
        return "whatsapp"
    if "email" in text or "SENT_FOLDER" in status or "RECIPIENT_SERVER" in status or "HISTORICAL_EMAIL" in status:
        return "email"
    return "legacy-unspecified"


def convert(source, output, review):
    if output.resolve() in {source.resolve(), review.resolve()} or source.resolve() == review.resolve():
        raise ValueError("源表、CSV和待复核记录须使用不同路径。")
    if output.exists() or review.exists():
        raise ValueError("CSV或复核文件已存在，请使用新的文件名。")
    original = json.loads(source.read_text(encoding="utf-8-sig"))
    records = original.get("leads")
    if not isinstance(records, list):
        raise ValueError("源文件须包含 leads 数组。")
    output_rows = []
    held = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            held.append({"row": index, "reason": "invalid-record"})
            continue
        company = str(record.get("company") or "").strip()
        status = str(record.get("status") or "").strip().upper()
        country = country_code(record.get("country"))
        if not company:
            held.append({"row": index, "reason": "missing-company"})
            continue
        if country == "ZZ":
            held.append({"row": index, "reason": "unverified-country", "company": company,
                         "domain": record.get("domain"), "status": status})
        aliases = record.get("aliases") or []
        identity = {
            "name": company, "country": country, "domain": record.get("domain") or "",
            "registration": (record.get("company_number") or record.get("abn") or "") if country != "ZZ" else "",
            "aliases": "|".join(a.strip() for a in aliases if isinstance(a, str) and a.strip())
                       if isinstance(aliases, list) else "",
        }
        base = f"{source.name} leads[{index - 1}]"
        states = [(channel_for(record, status), status)]
        for field, channel in (("email_status", "email"), ("whatsapp_status", "whatsapp")):
            if record.get(field):
                states.append((channel, str(record[field]).strip().upper()))
        for channel, old_status in dict.fromkeys(states):
            output_rows.append({**identity, "channel": channel, "state": state_for(old_status),
                                "source": f"{base} {old_status or 'STATUS_UNAVAILABLE'}"})
    with output.open("x", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(output_rows)
    with review.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(held, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return {"source_records": len(records), "csv_rows": len(output_rows), "needs_review": len(held),
            "coverage_claim": "none", "unknown_country_code": "ZZ",
            "csv": str(output.resolve()), "review": str(review.resolve())}


def main():
    parser = argparse.ArgumentParser(description="将本地旧去重主表转换为历史导入 CSV")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(convert(args.source, args.out, args.review), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
