"""
scripts/onboard_csm.py

Add a CSM to an existing org_config.json.
Run scripts/setup_org.py first if org_config.json doesn't exist yet.

Usage:
  python3 scripts/onboard_csm.py \
    --csm-id csm_001 \
    --name "First CSM" \
    --email "csm1@company.com" \
    --accounts "Account Alpha,Account Beta,Account Gamma"

  # CRM auto-discovery (CSM owns whatever the CRM assigns):
  python3 scripts/onboard_csm.py \
    --csm-id csm_002 \
    --name "Second CSM" \
    --email "csm2@company.com" \
    --accounts auto

  # Skip OAuth (set up token manually later):
  python3 scripts/onboard_csm.py ... --skip-oauth

  # Trigger first digest immediately:
  python3 scripts/onboard_csm.py ... --trigger-digest

  # Overwrite an existing CSM entry:
  python3 scripts/onboard_csm.py ... --force

Flags:
  --csm-id          CSM identifier used in env vars and file paths (required)
  --name            CSM display name (required)
  --email           CSM email address (required)
  --accounts        Comma-separated account names, or "auto" for CRM discovery
  --skip-oauth      Skip Gmail/Calendar OAuth flow
  --trigger-digest  Run the full digest pipeline immediately after onboarding
  --force           Overwrite an existing CSM entry without prompting
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent
ORG_CFG_PATH = REPO_ROOT / "org_config.json"
TOKENS_DIR   = REPO_ROOT / "tokens"

sys.path.insert(0, str(REPO_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_org_config_raw() -> dict:
    """Load org_config.json without resolving env var references."""
    if not ORG_CFG_PATH.exists():
        print("[onboard] Error: org_config.json not found.")
        print("[onboard] Run scripts/setup_org.py first to initialize the org.")
        sys.exit(1)
    with open(ORG_CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_org_config(config: dict) -> None:
    """Write org_config.json and bust the in-process cache."""
    ORG_CFG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        import product.org_config as _oc
        _oc._cached_config = None
    except Exception:
        pass


def _run_oauth_flow(csm_id: str) -> bool:
    """Run Gmail/Calendar OAuth flow for the CSM. Returns True if token exists."""
    token_path = TOKENS_DIR / f"{csm_id}.json"
    if token_path.exists():
        print(f"[onboard] OAuth token already exists at {token_path}")
        return True

    print(f"[onboard] No token found for '{csm_id}'. Running OAuth flow...")
    print("[onboard] A browser window will open for Gmail/Calendar authorization.")
    print("[onboard] Complete the authorization, then return here.\n")

    try:
        subprocess.run(
            [sys.executable, "gmail_auth_setup.py", csm_id],
            cwd=str(REPO_ROOT),
            check=True,
        )
        if token_path.exists():
            print(f"[onboard] Token saved to {token_path} ✓")
            return True
        else:
            print(f"[onboard] OAuth flow completed but token not found at {token_path}")
            return False
    except subprocess.CalledProcessError as e:
        print(f"[onboard] OAuth flow failed: {e}")
        return False


def _pull_crm_accounts(csm_id: str, crm_type: str) -> list[str]:
    """
    Pull account names for this CSM from the configured CRM.
    Returns [] if CRM is unconfigured, in mock mode, or on error.
    """
    if not crm_type:
        return []
    mock_env = f"{crm_type.upper()}_USE_MOCK"
    if os.environ.get(mock_env, "").lower() in ("1", "true", "yes"):
        print(f"[onboard] CRM is in mock mode ({mock_env}=true) — skipping account pull")
        return []
    try:
        from product.connectors.registry import get_connector
        connector    = get_connector(csm_id, {"crm": crm_type})
        accounts_raw = connector.get_accounts()
        names = [a.get("name", "") for a in accounts_raw if a.get("name")]
        print(f"[onboard] CRM returned {len(names)} account(s): {', '.join(names)}")
        return names
    except Exception as e:
        print(f"[onboard] CRM account pull failed — skipping: {e}")
        return []


def _ensure_customer_row(csm_id: str, name: str | None, email: str | None) -> None:
    """Idempotent insert of a customers row for the onboarded CSM.

    Source of truth for pilot_day calculation in /pilot/status after the
    System #1 retirement (Batch D). Safe to call repeatedly: checks for an
    existing row by clerk_user_id before inserting, and catches
    IntegrityError as a safety net for concurrent inserts.
    """
    from product.database import (
        Base, Customer, SessionLocal, engine, init_db,
    )
    from sqlalchemy.exc import IntegrityError

    if SessionLocal is None:
        init_db()
        from product.database import SessionLocal as _SL
        session_factory = _SL
    else:
        session_factory = SessionLocal

    if session_factory is None:
        print("[onboard] Warning: DATABASE_URL not configured, skipping customer row insert")
        return

    session = session_factory()
    try:
        existing = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
        if existing is not None:
            print(f"[onboard] customer row already exists for {csm_id}, skipping")
            return

        effective_email = email if email else f"{csm_id}@noreply.clerk"
        session.add(Customer(
            id=str(uuid.uuid4()),
            email=effective_email,
            name=name,
            clerk_user_id=csm_id,
            created_at=datetime.now(timezone.utc),
        ))
        try:
            session.commit()
            print(f"[onboard] customers row inserted for {csm_id}")
        except IntegrityError as _e:
            session.rollback()
            print(f"[onboard] customer row insert hit IntegrityError (race?), continuing: {_e}")
    finally:
        session.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Add a CSM to the CS Agent pilot.")
    parser.add_argument("--csm-id",  required=True, help="CSM identifier, e.g. csm_001")
    parser.add_argument("--name",    required=True, help="CSM display name, e.g. 'First CSM'")
    parser.add_argument("--email",   required=True, help="CSM email address")
    parser.add_argument(
        "--accounts",
        default="auto",
        help="Comma-separated account names, or 'auto' for CRM discovery (default: auto)",
    )
    parser.add_argument("--skip-oauth",      action="store_true", help="Skip Gmail/Calendar OAuth flow")
    parser.add_argument("--trigger-digest",  action="store_true", help="Run first digest immediately")
    parser.add_argument("--force",           action="store_true", help="Overwrite existing CSM entry")
    args = parser.parse_args()

    csm_id    = args.csm_id.strip()
    csm_name  = args.name.strip()
    csm_email = args.email.strip()

    # Parse accounts
    if args.accounts.strip().lower() == "auto":
        accounts: list[str] | str = "auto"
    else:
        accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]

    print(f"\n{'='*60}")
    print(f"CS Agent — CSM Onboarding")
    print(f"{'='*60}")
    print(f"  CSM:      {csm_name} ({csm_email})")
    print(f"  ID:       {csm_id}")
    print(f"  Accounts: {accounts if isinstance(accounts, str) else ', '.join(accounts)}")
    print(f"{'='*60}\n")

    # Load org config
    config   = _load_org_config_raw()
    crm_type = config.get("crm_type", "")

    # Check for existing CSM entry
    if csm_id in config.get("csms", {}):
        if not args.force:
            print(f"[onboard] Error: CSM '{csm_id}' already exists in org_config.json.")
            print(f"[onboard] Use --force to overwrite.")
            sys.exit(1)
        print(f"[onboard] Warning: overwriting existing entry for '{csm_id}' (--force)\n")

    # Step 1 — Write CSM entry to org_config
    print(f"[onboard] Step 1/5: Adding CSM to org_config.json...")
    if "csms" not in config:
        config["csms"] = {}
    config["csms"][csm_id] = {
        "name":          csm_name,
        "email":         csm_email,
        "accounts":      accounts,
        "notifications": {"email": True},
    }
    _save_org_config(config)
    print(f"[onboard] org_config.json updated ✓")

    # Step 2 — Gmail/Calendar OAuth
    token_status = "not found"
    if not args.skip_oauth:
        print("\n[onboard] Step 2/5: Gmail/Calendar OAuth...")
        oauth_ok = _run_oauth_flow(csm_id)
        token_status = "found" if oauth_ok else "not found (run gmail_auth_setup.py manually)"
        if not oauth_ok:
            print("[onboard] Warning: OAuth failed — CSM can still be onboarded but pipeline will not run until token is present.")
    else:
        print("[onboard] Step 2/5: OAuth skipped (--skip-oauth)")
        if (TOKENS_DIR / f"{csm_id}.json").exists():
            token_status = "found"

    # Step 3 — CRM account verification
    crm_accounts_count: int | str = "skipped (no CRM)"
    if crm_type:
        print(f"\n[onboard] Step 3/5: Verifying CRM accounts ({crm_type})...")
        crm_names = _pull_crm_accounts(csm_id, crm_type)
        if crm_names:
            crm_accounts_count = len(crm_names)
            # If accounts is "auto", note what the CRM returned
            if isinstance(accounts, str) and accounts == "auto":
                print(f"[onboard] Auto mode — CRM will supply {len(crm_names)} account(s) at runtime")
        elif os.environ.get(f"{crm_type.upper()}_USE_MOCK", "").lower() in ("1", "true", "yes"):
            crm_accounts_count = "skipped (mock mode)"
        else:
            crm_accounts_count = "error (check CRM credentials)"
    else:
        print("[onboard] Step 3/5: No CRM configured — skipping CRM verification")

    # Step 4 — Provision customer row in Postgres
    print("\n[onboard] Step 4/5: Provisioning customer row...")
    _ensure_customer_row(csm_id, csm_name, csm_email)
    print(f"[onboard] customers row ensured ✓")

    # Step 5 — Optionally trigger first digest
    if args.trigger_digest:
        print("\n[onboard] Step 5/5: Triggering first digest...")
        first_name = csm_name.split()[0].rstrip(".")
        result = subprocess.run(
            [
                sys.executable, "scripts/send_digest.py",
                "--csm",   csm_id,
                "--email", csm_email,
                "--name",  first_name,
            ],
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0:
            print("[onboard] First digest generated and saved ✓")
        else:
            print("[onboard] Warning: digest generation exited with errors — check output above")
    else:
        print("[onboard] Step 5/5: Digest skipped (omit --trigger-digest to skip)")

    # Generate suggested password
    password = secrets.token_urlsafe(16)
    env_var   = f"CSM_{csm_id.upper()}_PASSWORD"

    # Summary
    acct_display = (
        ", ".join(accounts) if isinstance(accounts, list) and accounts
        else ("auto (CRM discovery)" if accounts == "auto" else "(none)")
    )
    print(f"\n{'='*60}")
    print(f"Onboarding complete.")
    print(f"{'='*60}")
    print(f"  CSM added:          {csm_id} ({csm_name})")
    print(f"  Accounts:           {acct_display}")
    print(f"  Gmail token:        {token_status}")
    print(f"  CRM accounts:       {crm_accounts_count}")
    print(f"  Password env var:   {env_var}")
    print(f"{'='*60}")
    print(f"\nBefore this CSM can log in, set on Render:")
    print(f"  {env_var} = {password}")
    print(f"\n(Suggested password above — use any secure value you prefer.)\n")


if __name__ == "__main__":
    main()
