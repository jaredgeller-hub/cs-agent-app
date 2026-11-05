# gmail_auth_setup.py — run ONCE per CSM to authorize Gmail and Calendar API access
# Usage: python gmail_auth_setup.py <csm_id>
# Example: python gmail_auth_setup.py sarah_c

import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
]

def main(csm_id: str):
    creds_file = "gmail_credentials.json"

    if not os.path.exists(creds_file):
        print(f"❌ Missing {creds_file}. Download it from Google Cloud Console → APIs & Services → Credentials.")
        return

    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline")  # offline = refresh token issued

    os.makedirs("tokens", exist_ok=True)
    token_path = os.path.join("tokens", f"{csm_id}.json")
    with open(token_path, "w") as f:
        f.write(creds.to_json())

    print(f"✔ Token saved to {token_path} — Gmail + Calendar authorized for '{csm_id}'")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python gmail_auth_setup.py <csm_id>")
        print("Example: python gmail_auth_setup.py sarah_c")
        sys.exit(1)
    main(sys.argv[1])
