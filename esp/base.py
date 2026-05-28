"""ESP abstraction — every email platform implements this interface."""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class BaseESP(ABC):
    """
    sender / audience semantics differ per platform but the shape is the same:
    - sender:   who the email is from (GR fromFieldId / Klaviyo sender profile)
    - audience: who receives it (GR campaign/list / Klaviyo list id)
    Defaults come from the client config; explicit args override.
    """

    @abstractmethod
    def upload_image(self, image_bytes: bytes, filename: str, content_type: str = "") -> str:
        """Upload image bytes, return the hosted CDN URL."""

    @abstractmethod
    def create_draft(self, name: str, subject: str, html: str,
                     preheader: Optional[str] = None,
                     sender: Optional[str] = None,
                     audience: Optional[str] = None) -> Dict:
        """Create an editable draft. Returns {id, name, subject, status, href}."""

    @abstractmethod
    def list_senders(self) -> Dict:
        """List available sender identities."""

    @abstractmethod
    def list_audiences(self) -> Dict:
        """List available lists/audiences."""

    @abstractmethod
    def list_drafts(self, name_filter: Optional[str] = None) -> Dict:
        """List existing drafts."""

    @abstractmethod
    def delete_drafts(self, ids: List[str]) -> Dict:
        """Delete drafts by id."""
