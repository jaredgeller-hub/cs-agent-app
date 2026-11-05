"""
product/connectors/base.py

Abstract base class defining what CS Agent expects from any CRM/CSP connector.
All connectors normalize to this interface — the intelligence pipeline never
knows which CRM is active.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class CRMConnector(ABC):
    """Base class for all CRM/CSP connectors."""

    def __init__(self, csm_id: str, config: dict):
        self.csm_id = csm_id
        self.config = config

    @abstractmethod
    def get_accounts(self) -> list[dict]:
        """Return accounts assigned to this CSM.

        Each dict: {name, tier, owner, id}
        """

    @abstractmethod
    def get_contacts(self, account_name: str) -> list[dict]:
        """Return contacts for a given account.

        Called once per account — not a bulk endpoint.
        Each dict: {name, email, role, account_name}
        """

    @abstractmethod
    def get_renewals(self) -> list[dict]:
        """Return accounts with renewal information.

        Each dict: {account_name, renewal_date, contract_value, days_until_renewal}
        """

    @abstractmethod
    def get_health_scores(self) -> list[dict]:
        """Return health scores for all accounts.

        Each dict: {account_name, score, trend, last_updated}
        trend is one of: 'improving', 'stable', 'declining'
        """

    @abstractmethod
    def get_open_ctas(self) -> list[dict]:
        """Return open CTAs/tasks across all accounts.

        Each dict: {account_name, title, due_date, priority, status, owner}
        status is one of: 'overdue', 'due_today', 'upcoming'
        """

    @abstractmethod
    def get_recent_activity(self, account_name: str, days: int = 14) -> list[dict]:
        """Return recent timeline entries for an account.

        Each dict: {account_name, type, summary, date, author}
        """
