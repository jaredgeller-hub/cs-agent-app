"""
product/connectors/hubspot.py
"""

from __future__ import annotations

from product.settings import SUPPORT_EMAIL

from .base import CRMConnector

_NOT_IMPLEMENTED = f"HubSpot connector not yet implemented. Contact {SUPPORT_EMAIL} to enable."


class HubSpotConnector(CRMConnector):
    def get_accounts(self) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_contacts(self, account_name: str) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_renewals(self) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_health_scores(self) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_open_ctas(self) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_recent_activity(self, account_name: str, days: int = 14) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)
