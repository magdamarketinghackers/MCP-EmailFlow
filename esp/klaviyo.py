"""
Klaviyo ESP implementation.

Draft flow (Klaviyo has no single 'draft' object like GR):
  1. POST /api/images/        — host image (import_from_url accepts base64 data URI)
  2. POST /api/templates/     — store HTML as a template
  3. POST /api/campaigns/     — create campaign (created in draft status)
  4. POST /api/campaign-message-assign-template/ — attach the template to the message

Auth: header  Authorization: Klaviyo-API-Key pk_xxx
      header  revision: <date>   (date-based API versioning)
"""
import base64
import json
import logging
from typing import Dict, List, Optional

import httpx

from .base import BaseESP

jsonlib_dumps = json.dumps

logger = logging.getLogger(__name__)

KLAVIYO_BASE = "https://a.klaviyo.com/api"
REVISION = "2024-10-15"
# Klaviyo API uses the JSON:API spec — mandatory content-type for POST/PATCH/DELETE.
JSONAPI_MIME = "application/vnd.api+json"


class KlaviyoESP(BaseESP):
    def __init__(self, api_key: str,
                 default_sender: Optional[str] = None,
                 default_audience: Optional[str] = None,
                 from_label: Optional[str] = None):
        self.api_key = api_key
        # default_sender / from_label still accepted as explicit overrides,
        # but normally we pull from the account itself (see _account_sender).
        self.default_sender = default_sender
        self.default_audience = default_audience
        self.from_label = from_label
        self._cached_acct_sender: Optional[Dict[str, str]] = None

    def _headers(self, write: bool = True) -> Dict[str, str]:
        """write=True for POST/PATCH/DELETE bodies (uses JSON:API mime type).
           write=False for GETs (regular json accept is fine)."""
        h = {
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "revision": REVISION,
            "accept": JSONAPI_MIME,
        }
        if write:
            h["content-type"] = JSONAPI_MIME
        return h

    def _account_sender(self) -> Dict[str, str]:
        """Fetch the account's default sender (from_email + from_label).
           Cached for the lifetime of this ESP instance."""
        if self._cached_acct_sender is not None:
            return self._cached_acct_sender
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{KLAVIYO_BASE}/accounts/", headers=self._headers(write=False))
            if r.status_code >= 400:
                raise RuntimeError(f"Klaviyo accounts {r.status_code}: {r.text[:300]}")
            payload = r.json()
        rows = payload.get("data", [])
        if not rows:
            raise RuntimeError("Klaviyo /accounts returned no rows")
        ci = rows[0].get("attributes", {}).get("contact_information", {}) or {}
        self._cached_acct_sender = {
            "from_email": ci.get("default_sender_email") or "",
            "from_label": ci.get("default_sender_name") or "",
        }
        return self._cached_acct_sender

    # ── images ──────────────────────────────────────────────────────────────
    def upload_image(self, image_bytes: bytes, filename: str, content_type: str = "") -> str:
        name = filename.rsplit(".", 1)[0]
        mime = content_type or "image/png"
        data_uri = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        payload = {
            "data": {
                "type": "image",
                "attributes": {"import_from_url": data_uri, "name": name, "hidden": False},
            }
        }
        with httpx.Client(timeout=120) as c:
            r = c.post(f"{KLAVIYO_BASE}/images/", headers=self._headers(), json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"Klaviyo image upload {r.status_code}: {r.text[:400]}")
            data = r.json()
        url = (data.get("data", {}).get("attributes", {}) or {}).get("image_url")
        if not url:
            raise RuntimeError(f"Klaviyo image ok but no image_url: {str(data)[:300]}")
        return url

    # ── draft ───────────────────────────────────────────────────────────────
    def create_draft(self, name, subject, html, preheader=None, sender=None, audience=None) -> Dict:
        audience = audience or self.default_audience
        if not audience:
            return {"error": "No audience (Klaviyo list id) — set client default or pass audience"}

        # Resolve sender + from_label: explicit > stored default > account default
        from_label = self.from_label
        if not sender:
            sender = self.default_sender
        if not sender or not from_label:
            try:
                acct = self._account_sender()
            except Exception as e:
                return {"error": f"Could not fetch Klaviyo account sender: {e}. "
                                 "Grant 'Accounts: Read Only' scope on the API key, "
                                 "or pass `sender` explicitly."}
            sender = sender or acct["from_email"]
            from_label = from_label or acct["from_label"]
        if not sender:
            return {"error": "No sender (from_email) — set it in Klaviyo Account "
                             "→ Settings → Contact Information or pass `sender` explicitly"}

        # Klaviyo's `preview_text` field only works for drag-and-drop templates.
        # For Custom HTML templates we must inject the preheader as a hidden span
        # at the start of the HTML (same trick as GR).
        if preheader:
            span = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
                    f'{preheader}</div>')
            html = html.replace("<table", span + "<table", 1) if "<table" in html else span + html

        try:
            with httpx.Client(timeout=60) as c:
                # 1. template
                tpl_payload = {"data": {"type": "template", "attributes": {
                    "name": name, "editor_type": "CODE", "html": html}}}
                tpl = c.post(f"{KLAVIYO_BASE}/templates/", headers=self._headers(),
                             json=tpl_payload)
                if tpl.status_code >= 400:
                    logger.error(f"Klaviyo template {tpl.status_code}: {tpl.text}")
                    return {"error": f"Klaviyo template {tpl.status_code}: {tpl.text[:1500]}"}
                template_id = tpl.json()["data"]["id"]

                # 2. campaign (draft) with one email message.
                # NOTE: channel + label + content sit directly on the message's `attributes`
                # (flat structure). The OpenAPI 'definition' wrapper applies to omni
                # revisions; for the stable/non-omni revision Klaviyo expects flat.
                camp_payload = {"data": {"type": "campaign", "attributes": {
                    "name": name,
                    "audiences": {"included": [audience], "excluded": []},
                    "campaign-messages": {"data": [{
                        "type": "campaign-message",
                        "attributes": {
                            "channel": "email",
                            "label": name,
                            # preview_text omitted on purpose — it only renders for
                            # drag-and-drop templates, and we use Custom HTML.
                            # The preheader is injected as a hidden span in the HTML above.
                            "content": {
                                "subject": subject,
                                "from_email": sender,
                                "from_label": from_label or sender,
                            },
                        },
                    }]},
                }}}
                camp = c.post(f"{KLAVIYO_BASE}/campaigns/", headers=self._headers(),
                              json=camp_payload)
                if camp.status_code >= 400:
                    logger.error(f"Klaviyo campaign {camp.status_code}: {camp.text}")
                    logger.error(f"Payload sent: {jsonlib_dumps(camp_payload)[:1500]}")
                    return {"error": f"Klaviyo campaign {camp.status_code}: {camp.text[:1500]}"}
                cdata = camp.json()["data"]
                campaign_id = cdata["id"]
                msg_id = (cdata.get("relationships", {}).get("campaign-messages", {})
                          .get("data", [{}])[0].get("id"))

                # 3. attach template to the message
                if msg_id:
                    assign = c.post(f"{KLAVIYO_BASE}/campaign-message-assign-template/",
                                    headers=self._headers(), json={"data": {
                                        "type": "campaign-message", "id": msg_id,
                                        "relationships": {"template": {"data": {
                                            "type": "template", "id": template_id}}}}})
                    if assign.status_code >= 400:
                        logger.error(f"Klaviyo assign-template {assign.status_code}: {assign.text}")
                        return {"error": f"Klaviyo assign-template {assign.status_code}: "
                                         f"{assign.text[:1500]} (campaign {campaign_id} created)"}

            return {
                "id": campaign_id, "name": name, "subject": subject,
                "status": "draft", "template_id": template_id,
                "href": f"https://www.klaviyo.com/campaign/{campaign_id}/edit",
            }
        except Exception as e:
            return {"error": str(e)}

    # ── lists ───────────────────────────────────────────────────────────────
    def list_senders(self) -> Dict:
        """
        Klaviyo has no multi-sender list — there is one default sender on the account.
        We return it as a single entry so the wizard can display it (read-only).
        """
        try:
            acct = self._account_sender()
        except Exception as e:
            return {"error": str(e)}
        if not acct["from_email"]:
            return {"senders": [],
                    "note": "No default sender on Klaviyo account. "
                            "Set it in Klaviyo → Account → Settings → Contact Information."}
        return {"senders": [{
            "id": acct["from_email"],
            "email": acct["from_email"],
            "name": acct["from_label"],
            "isDefault": True,
        }], "account_default": True}

    def list_audiences(self) -> Dict:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{KLAVIYO_BASE}/lists/", headers=self._headers(write=False))
            r.raise_for_status()
        out = [{"id": l["id"], "name": l.get("attributes", {}).get("name")}
               for l in r.json().get("data", [])]
        return {"audiences": out, "count": len(out)}

    def list_drafts(self, name_filter: Optional[str] = None) -> Dict:
        params = {"filter": "equals(messages.channel,'email')"}
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{KLAVIYO_BASE}/campaigns/", headers=self._headers(write=False), params=params)
            r.raise_for_status()
        out = []
        for d in r.json().get("data", []):
            attrs = d.get("attributes", {})
            nm = attrs.get("name", "")
            if name_filter and name_filter.lower() not in nm.lower():
                continue
            out.append({"id": d["id"], "name": nm, "status": attrs.get("status")})
        return {"drafts": out, "count": len(out)}

    def delete_drafts(self, ids: List[str]) -> Dict:
        deleted, errors = [], {}
        with httpx.Client(timeout=30) as c:
            for cid in ids:
                try:
                    r = c.delete(f"{KLAVIYO_BASE}/campaigns/{cid}/", headers=self._headers(write=False))
                    if r.status_code in (200, 204):
                        deleted.append(cid)
                    else:
                        errors[cid] = f"HTTP {r.status_code}: {r.text[:150]}"
                except Exception as e:
                    errors[cid] = str(e)
        return {"deleted": deleted, "errors": errors,
                "success": len(deleted), "failed": len(errors)}
