"""
product/connectors/hubspot_write.py

HubSpot write-back via REST API v3.
All write operations must go through /app/actions/execute (human-in-the-loop).
Uses the same OAuth token managed by hubspot_mcp.py.

Mock mode: set HUBSPOT_USE_MOCK=true to simulate writes without hitting the API.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import requests

from product.connectors.hubspot_mcp import (
    load_hubspot_token,
    refresh_hubspot_token,
    save_hubspot_token,
)

BASE_URL = "https://api.hubapi.com"


class HubSpotWriter:
    def __init__(self, csm_id: str):
        self.csm_id = csm_id
        self.mock_mode = os.environ.get("HUBSPOT_USE_MOCK", "").lower() in ("1", "true", "yes")
        if not self.mock_mode:
            self._load_token()

    def _load_token(self):
        token_data = load_hubspot_token(self.csm_id)
        if not token_data:
            raise ValueError(f"No HubSpot token for {self.csm_id}")
        # Refresh if expired or within 60s of expiry
        if token_data.get("expires_at", 0) < time.time() + 60:
            from product.token_lock import token_lock
            with token_lock(self.csm_id, "hubspot"):
                # Double-check with the SAME 60s threshold
                token_data = load_hubspot_token(self.csm_id)
                if token_data and token_data.get("expires_at", 0) < time.time() + 60:
                    token_data = refresh_hubspot_token(token_data["refresh_token"])
                    save_hubspot_token(self.csm_id, token_data)
        self.access_token = token_data["access_token"]
        self.token_data = token_data

    def _request(self, method: str, path: str, json_body: dict = None) -> dict:
        """Make an authenticated request to HubSpot REST API. Retries once on 401."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        url = f"{BASE_URL}{path}"
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=15)

        if resp.status_code == 401:
            self._load_token()
            headers["Authorization"] = f"Bearer {self.access_token}"
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=15)

        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def log_note(self, company_id: str, note_body: str) -> dict:
        """Create a note on a company record."""
        if self.mock_mode:
            mock_id = f"mock-note-{uuid.uuid4().hex[:8]}"
            print(f"[hubspot_write/mock] log_note company={company_id!r} body={note_body[:80]!r}")
            return {"status": "created", "note_id": mock_id}

        payload = {
            "properties": {
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
                "hs_note_body": note_body,
            }
        }
        result = self._request("POST", "/crm/v3/objects/notes", payload)
        note_id = result.get("id")

        if note_id and company_id:
            self._request(
                "PUT",
                f"/crm/v3/objects/notes/{note_id}/associations/companies/{company_id}/note_to_company",
            )

        return {"status": "created", "note_id": note_id}

    def create_task(
        self,
        title: str,
        due_date: str = None,
        company_id: str = None,
        notes: str = None,
    ) -> dict:
        """Create a task, optionally associated with a company."""
        if self.mock_mode:
            mock_id = f"mock-task-{uuid.uuid4().hex[:8]}"
            print(f"[hubspot_write/mock] create_task title={title!r} due={due_date!r} company={company_id!r}")
            return {"status": "created", "task_id": mock_id}

        properties = {
            "hs_task_subject": title,
            "hs_task_status": "NOT_STARTED",
            "hs_task_priority": "MEDIUM",
            "hs_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if due_date:
            properties["hs_task_reminderat"] = due_date
        if notes:
            properties["hs_task_body"] = notes

        result = self._request("POST", "/crm/v3/objects/tasks", {"properties": properties})
        task_id = result.get("id")

        if task_id and company_id:
            self._request(
                "PUT",
                f"/crm/v3/objects/tasks/{task_id}/associations/companies/{company_id}/task_to_company",
            )

        return {"status": "created", "task_id": task_id}

    def update_deal(self, deal_id: str, properties: dict) -> dict:
        """Update deal properties (stage, amount, close date, etc.)."""
        if self.mock_mode:
            print(f"[hubspot_write/mock] update_deal deal_id={deal_id!r} properties={properties!r}")
            return {"status": "updated", "deal_id": deal_id}

        result = self._request(
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            {"properties": properties},
        )
        return {"status": "updated", "deal_id": result.get("id")}

    def create_contact(
        self,
        email: str,
        firstname: str = None,
        lastname: str = None,
        company_id: str = None,
    ) -> dict:
        """Create or update a contact."""
        if self.mock_mode:
            mock_id = f"mock-contact-{uuid.uuid4().hex[:8]}"
            print(f"[hubspot_write/mock] create_contact email={email!r} company={company_id!r}")
            return {"status": "created", "contact_id": mock_id}

        properties: dict = {"email": email}
        if firstname:
            properties["firstname"] = firstname
        if lastname:
            properties["lastname"] = lastname

        result = self._request("POST", "/crm/v3/objects/contacts", {"properties": properties})
        contact_id = result.get("id")

        if contact_id and company_id:
            self._request(
                "PUT",
                f"/crm/v3/objects/contacts/{contact_id}/associations/companies/{company_id}/contact_to_company",
            )

        return {"status": "created", "contact_id": contact_id}
