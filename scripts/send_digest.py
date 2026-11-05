"""
scripts/send_digest.py

CLI script to manually trigger a morning digest for a CSM.
Used for testing Phase 1 end-to-end before APScheduler is wired in Phase 4.

Usage:
  python3 scripts/send_digest.py --csm sarah_c --email demo@example.com

Optional flags:
  --dry-run   Build and print the digest without sending the email
  --accounts  Comma-separated list of account names (overrides default)
"""

import argparse
import json
import sys
from pathlib import Path

# Add repo root to path so product/ imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

from product.signals.gmail import extract_gmail_signals
from product.signals.calendar import extract_calendar_signals
from product.signals.detector import detect_and_classify, detect_crm_signals
from product.digest.builder import build_morning_digest
from product.digest.delivery import send_digest_email, send_notification_email, save_digest_to_disk
from product.connectors.registry import get_connector


# ── Default account contacts ──────────────────────────────────────────────────
# This will be replaced by a config file or CRM lookup in Phase 4.
# For now, hardcoded for sarah_c pilot.

DEFAULT_ACCOUNT_CONTACTS = {
    "Meridian Health": ["marcus.reyes@meridianhealth.example"],
    "Beacon Financial": ["priya.sharma@beaconfinancial.example"],
    "Sutton Retail": ["contact@suttonretail.example"],
    "Northgate Logistics": ["dana.mills@northgate.example"],
    "Pinnacle Group": ["contact@pinnaclegroup.example"],
}

DEFAULT_ACCOUNT_NAMES = list(DEFAULT_ACCOUNT_CONTACTS.keys())


# ── Store loader ──────────────────────────────────────────────────────────────

def load_store_data() -> dict:
    """Load account summaries and CTAs from store.json."""
    store_path = Path(__file__).parent.parent / "store.json"
    if not store_path.exists():
        print("[send_digest] Warning: store.json not found — using empty store")
        return {"summaries": []}
    with open(store_path) as f:
        return json.load(f)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Send a CS Agent morning digest.")
    parser.add_argument("--csm", required=True, help="CSM ID (e.g. sarah_c)")
    parser.add_argument("--email", required=True, help="CSM email address")
    parser.add_argument("--name", default="Sarah", help="CSM display name (default: Sarah)")
    parser.add_argument("--dry-run", action="store_true", help="Print digest without sending")
    parser.add_argument(
        "--accounts",
        help="Comma-separated account names (overrides default list)",
    )
    args = parser.parse_args()

    # Build account list
    if args.accounts:
        account_names = [a.strip() for a in args.accounts.split(",")]
        account_contacts = {name: DEFAULT_ACCOUNT_CONTACTS.get(name, []) for name in account_names}
    else:
        account_contacts = DEFAULT_ACCOUNT_CONTACTS
        account_names = DEFAULT_ACCOUNT_NAMES

    # Load pilot_config if available
    pilot_config_path = Path(__file__).parent.parent / "pilot_config.json"
    pilot_config = {}
    pilot_config_exists = pilot_config_path.exists()
    if pilot_config_exists:
        try:
            with open(pilot_config_path) as f:
                pilot_config = json.load(f)
        except Exception as e:
            print(f"[send_digest] Warning: could not load pilot_config.json: {e}")
            pilot_config_exists = False

    print(f"\n[send_digest] Starting digest pipeline for {args.csm}...")
    print(f"[send_digest] Accounts: {', '.join(account_names)}\n")

    # Step 1 — Gmail signals
    print("[send_digest] Step 1/5: Extracting Gmail signals...")
    try:
        gmail_signals = extract_gmail_signals(args.csm, account_contacts)
        print(f"[send_digest] Found {len(gmail_signals)} Gmail signals")
    except Exception as e:
        print(f"[send_digest] Gmail extraction failed: {e}")
        gmail_signals = []

    # Step 2 — Calendar signals
    print("[send_digest] Step 2/5: Extracting Calendar signals...")
    try:
        calendar_signals = extract_calendar_signals(args.csm, account_names)
        print(f"[send_digest] Found {calendar_signals.get('total_customer_meetings', 0)} customer meetings today")
    except Exception as e:
        print(f"[send_digest] Calendar extraction failed: {e}")
        calendar_signals = {
            "date": "",
            "customer_meetings": [],
            "internal_meetings": [],
            "total_customer_meetings": 0,
            "total_meetings": 0,
            "available_gaps": [],
        }

    # Step 2b — CRM signals
    print("[send_digest] Step 2b: Loading CRM signals...")
    crm_signals = []
    if pilot_config.get("crm"):
        try:
            connector = get_connector(args.csm, pilot_config)
            # Scope to the dev script's account list (CLI flag or DEFAULT_ACCOUNT_NAMES).
            # Same confidentiality posture as the production digest path: a connector
            # whose get_renewals/get_open_ctas queries are portfolio-wide must not
            # surface another CSM's accounts in this user's digest.
            crm_signals = detect_crm_signals(
                connector,
                allowed_accounts=set(account_names),
            )
            print(f"[send_digest] Found {len(crm_signals)} CRM signals (owned={len(account_names)})")
        except Exception as e:
            print(f"[send_digest] CRM signals failed — continuing without: {e}")
    else:
        print("[send_digest] No CRM configured in pilot_config.json — skipping")

    # Step 3 — Classify signals
    print("[send_digest] Step 3/5: Classifying signals...")
    store_data = load_store_data()
    classified = detect_and_classify(gmail_signals, calendar_signals, [], store_data)
    at_risk = [a for a in classified.get("priority_accounts", []) if a["risk_level"] == "at_risk"]
    print(f"[send_digest] {len(classified.get('priority_accounts', []))} accounts with signals, {len(at_risk)} at risk")

    # Step 4 — Build digest
    print("[send_digest] Step 4/5: Building digest with LLM...")
    digest = build_morning_digest(classified, args.name, crm_signals=crm_signals)
    print(f"[send_digest] Digest built ({len(digest.get('digest_text', ''))} chars)")

    # Print preview
    print("\n" + "─" * 60)
    print(digest.get("greeting", ""))
    print()
    print(digest.get("digest_text", ""))
    print("─" * 60 + "\n")

    # Step 4b — Save digest to disk (always, including dry runs)
    print("[send_digest] Step 4b: Saving digest to disk...")
    try:
        digest_path = save_digest_to_disk(digest, args.csm)
        print(f"[send_digest] Saved to {digest_path}")
    except Exception as e:
        print(f"[send_digest] Warning: could not save digest to disk: {e}")

    # Step 5 — Send email (or skip based on pilot_config)
    if args.dry_run:
        print("[send_digest] DRY RUN — email not sent.")
        return

    email_notifications = pilot_config.get("email_notifications", True)
    print(f"[send_digest] Email path: pilot_config_exists={pilot_config_exists}, email_notifications={email_notifications}")

    if pilot_config_exists:
        if email_notifications is False:
            print("[send_digest] Email notifications disabled in pilot_config — skipping.")
        else:
            print(f"[send_digest] Step 5/5: Sending notification email to {args.email}...")
            result = send_notification_email(digest, args.email, args.csm)
            print(f"[send_digest] Sent. message_id={result.get('message_id')}, sent_at={result.get('sent_at')}")
    else:
        print(f"[send_digest] Step 5/5: Sending full digest to {args.email}...")
        result = send_digest_email(digest, args.email, args.csm)
        print(f"[send_digest] Sent. message_id={result.get('message_id')}, sent_at={result.get('sent_at')}")

    print("\n[send_digest] Pipeline complete.\n")


if __name__ == "__main__":
    main()
