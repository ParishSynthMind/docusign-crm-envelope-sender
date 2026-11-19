"""CRM adapters: HubSpot, GoHighLevel and Zoho CRM behind one small interface."""

from .base import CRMError, Deal, StageEvent
from .ghl import GHLAdapter
from .hubspot import HubSpotAdapter
from .zoho import ZohoAdapter

__all__ = ["CRMError", "Deal", "StageEvent", "GHLAdapter", "HubSpotAdapter", "ZohoAdapter"]
