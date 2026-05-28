"""ESP factory — build the right ESP client from a stored client config."""
from typing import Dict

from .base import BaseESP
from .getresponse import GetResponseESP, DEFAULT_BASE
from .klaviyo import KlaviyoESP

SUPPORTED = ("getresponse", "klaviyo")


def get_esp(client: Dict) -> BaseESP:
    """
    client: dict from store.get_client() with decrypted credentials.
      { esp_type, credentials: {...}, defaults: {...} }
    """
    esp_type = client["esp_type"]
    creds = client.get("credentials", {})
    defaults = client.get("defaults", {})

    if esp_type == "getresponse":
        return GetResponseESP(
            api_key=creds["api_key"],
            base=creds.get("base", DEFAULT_BASE),
            default_sender=defaults.get("sender"),
            default_audience=defaults.get("audience"),
        )
    if esp_type == "klaviyo":
        return KlaviyoESP(
            api_key=creds["api_key"],
            default_sender=defaults.get("sender"),
            default_audience=defaults.get("audience"),
            from_label=defaults.get("from_label"),
        )
    raise ValueError(f"Unsupported esp_type: {esp_type} (supported: {SUPPORTED})")
