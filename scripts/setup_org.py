"""
scripts/setup_org.py

One-time org initialization for CS Agent multi-CSM pilot.
Creates org_config.json in the repo root with org-level settings.
CSMs are added afterward via scripts/onboard_csm.py.

Usage:
  python3 scripts/setup_org.py \
    --org-name "Pilot Org" \
    --crm planhat \
    --timezone "America/Chicago"

  # With secondary CRM:
  python3 scripts/setup_org.py \
    --org-name "Pilot Org" \
    --crm planhat \
    --secondary-crm hubspot \
    --timezone "America/Chicago"

  # Overwrite existing config:
  python3 scripts/setup_org.py --org-name "Pilot Org" --crm planhat --force

Flags:
  --org-name        Display name for this org (required)
  --crm             Primary CRM: planhat | salesforce | hubspot | none (default: none)
  --secondary-crm   Secondary CRM: same choices (optional)
  --timezone        Org-wide timezone, e.g. "America/Chicago" (default: America/New_York)
  --digest-time     Daily digest time in HH:MM (default: 08:00)
  --force           Overwrite existing org_config.json without prompting
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent
ORG_CFG_PATH = REPO_ROOT / "org_config.json"

CRM_CHOICES = ["planhat", "salesforce", "hubspot", "gainsight", "totango", "churnzero", "none"]

# Maps CRM name -> expected env var for credentials
_CRM_ENV_VARS: dict[str, str] = {
    "planhat":   "PLANHAT_API_KEY",
    "salesforce": "SALESFORCE_CLIENT_ID",
    "hubspot":   "HUBSPOT_API_KEY",
    "gainsight": "GAINSIGHT_API_KEY",
    "totango":   "TOTANGO_API_KEY",
    "churnzero": "CHURNZERO_API_KEY",
}


def _validate_timezone(tz: str) -> bool:
    """Return True if tz is a valid IANA timezone identifier."""
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(tz)
            return True
        except (ZoneInfoNotFoundError, KeyError):
            return False
    except ImportError:
        # Python < 3.9 or tzdata not installed — try pytz
        try:
            import pytz
            pytz.timezone(tz)
            return True
        except Exception:
            return False


def _crm_credentials(crm: str) -> dict | None:
    """Return the credentials env-var reference for the given CRM."""
    if crm == "none" or crm not in _CRM_ENV_VARS:
        return None
    return {"env": _CRM_ENV_VARS[crm]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize org-level config for CS Agent multi-CSM pilot."
    )
    parser.add_argument("--org-name",      required=True, help="Display name for this org")
    parser.add_argument(
        "--crm",
        default="none",
        choices=CRM_CHOICES,
        help="Primary CRM platform (default: none)",
    )
    parser.add_argument(
        "--secondary-crm",
        default="none",
        choices=CRM_CHOICES,
        dest="secondary_crm",
        help="Secondary CRM platform (optional)",
    )
    parser.add_argument(
        "--timezone",
        default="America/New_York",
        help="Org-wide IANA timezone (default: America/New_York)",
    )
    parser.add_argument(
        "--digest-time",
        default="08:00",
        dest="digest_time",
        help="Daily digest time HH:MM (default: 08:00)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing org_config.json without prompting",
    )
    args = parser.parse_args()

    # Validate timezone
    if not _validate_timezone(args.timezone):
        print(f"[setup_org] Error: '{args.timezone}' is not a valid IANA timezone.")
        print("[setup_org] Examples: America/Chicago, America/New_York, Europe/London, UTC")
        sys.exit(1)

    # Check for existing config
    if ORG_CFG_PATH.exists() and not args.force:
        print(f"[setup_org] org_config.json already exists at {ORG_CFG_PATH}")
        try:
            answer = input("[setup_org] Overwrite? This will clear all existing CSM entries. [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer != "y":
            print("[setup_org] Aborted. Use --force to skip this prompt.")
            sys.exit(0)
        print()

    # Build org_config structure
    config: dict = {
        "org_name":  args.org_name,
        "crm_type":  args.crm if args.crm != "none" else "",
        "timezone":  args.timezone,
        "digest_time": args.digest_time,
        "absence_check_times": ["08:00", "12:00", "16:00"],
        "csms": {},
    }

    crm_creds = _crm_credentials(args.crm)
    if crm_creds:
        config["crm_credentials"] = crm_creds

    sec_crm = args.secondary_crm
    if sec_crm and sec_crm != "none":
        config["secondary_crm_type"] = sec_crm
        sec_creds = _crm_credentials(sec_crm)
        if sec_creds:
            config["secondary_crm_credentials"] = sec_creds

    ORG_CFG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Print summary
    print(f"\n{'='*60}")
    print(f"CS Agent — Org Setup")
    print(f"{'='*60}")
    print(f"  Org name:     {args.org_name}")
    print(f"  Primary CRM:  {args.crm}")
    if sec_crm and sec_crm != "none":
        print(f"  Secondary CRM: {sec_crm}")
    print(f"  Timezone:     {args.timezone}")
    print(f"  Digest time:  {args.digest_time}")
    print(f"  Config path:  {ORG_CFG_PATH}")
    print(f"  CSMs:         0 (add with scripts/onboard_csm.py)")
    print(f"{'='*60}\n")

    if crm_creds:
        env_var = _CRM_ENV_VARS[args.crm]
        print(f"[setup_org] Set on Render: {env_var}=<your-api-key>")
    if sec_crm and sec_crm != "none" and sec_crm in _CRM_ENV_VARS:
        sec_env = _CRM_ENV_VARS[sec_crm]
        print(f"[setup_org] Set on Render: {sec_env}=<your-api-key>")

    print(f"\n[setup_org] Done. Next step: python3 scripts/onboard_csm.py --csm-id <id> ...\n")


if __name__ == "__main__":
    main()
