"""
scripts/remove_csm.py

Remove a CSM from the active pilot (org_config.json).
Data (digests, alerts, tokens) is NOT deleted — only the config entry is removed.

Usage:
  python3 scripts/remove_csm.py --csm-id csm_001 --force

Flags:
  --csm-id    CSM identifier to remove (required)
  --force     Required safety flag — prevents accidental removal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent
ORG_CFG_PATH = REPO_ROOT / "org_config.json"

sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove a CSM from the CS Agent pilot.")
    parser.add_argument("--csm-id", required=True, help="CSM identifier to remove, e.g. csm_001")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Required safety flag — prevents accidental removal",
    )
    args = parser.parse_args()

    csm_id = args.csm_id.strip()

    if not args.force:
        print(f"[remove_csm] Error: --force flag is required to remove a CSM.")
        print(f"[remove_csm] This removes '{csm_id}' from org_config.json.")
        print(f"[remove_csm] Data files (digests, alerts, tokens) are NOT deleted.")
        print(f"[remove_csm] Re-run with --force to confirm.")
        sys.exit(1)

    if not ORG_CFG_PATH.exists():
        print(f"[remove_csm] Error: org_config.json not found at {ORG_CFG_PATH}")
        print(f"[remove_csm] Nothing to remove.")
        sys.exit(1)

    with open(ORG_CFG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    csms = config.get("csms", {})
    if csm_id not in csms:
        print(f"[remove_csm] Error: CSM '{csm_id}' not found in org_config.json.")
        print(f"[remove_csm] Active CSMs: {list(csms.keys()) or '(none)'}")
        sys.exit(1)

    removed = csms.pop(csm_id)
    config["csms"] = csms

    ORG_CFG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Bust in-process cache
    try:
        import product.org_config as _oc
        _oc._cached_config = None
    except Exception:
        pass

    remaining = list(csms.keys())
    print(f"\n[remove_csm] Removed: {csm_id} ({removed.get('name', '')})")
    print(f"[remove_csm] Remaining CSMs: {remaining or '(none)'}")
    print(f"[remove_csm] Data files (digests, alerts) were NOT deleted.")
    print(f"[remove_csm] OAuth tokens are now Postgres-only (Stage 2a). To fully offboard,")
    print(f"[remove_csm] confirm the connections row for csm_id={csm_id} is gone "
          f"(use product.token_store.delete_token or a direct DB delete).\n")


if __name__ == "__main__":
    main()
