"""
product/connectors/__init__.py

Public surface for the connector package.
"""

from .base import CRMConnector
from .registry import get_connector

__all__ = ["CRMConnector", "get_connector"]
