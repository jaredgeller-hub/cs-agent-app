"""
product/connectors/registry.py

Returns the correct CRMConnector implementation based on config.
Connector imports are deferred so a missing dependency for one platform
doesn't crash the app when a different platform is in use.
"""

from __future__ import annotations

from .base import CRMConnector


def get_connector(csm_id: str, config: dict) -> CRMConnector:
    """Instantiate and return the connector for the configured CRM platform.

    Args:
        csm_id: CSM identifier, e.g. "sarah_c"
        config: Dict containing at minimum {"crm": "<platform>"}.
                Platform-specific keys (api keys, field maps, etc.) are
                passed through to the connector unchanged.

    Raises:
        ValueError: If the platform is not recognized.
    """
    platform = config.get("crm", "").lower()

    if platform == "gainsight":
        from .gainsight import GainsightCRMConnector
        return GainsightCRMConnector(csm_id, config)

    elif platform == "hubspot":
        from .hubspot_crm import HubSpotCRMConnector
        return HubSpotCRMConnector(csm_id)

    else:
        raise ValueError(f"Unknown CRM platform: {platform}")
