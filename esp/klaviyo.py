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
import logging
from typing import Dict, List, Optional

import httpx

from .base import BaseESP

logger = logging.getLogger(__name__)

KLAVIYO_BASE = "https://a.klaviyo.com/api"
REVISION = "2024-10-15"


class KlaviyoESP(BaseESP):
    def __init__(self, api_key: str,
                 default_sender: Optional[str] = None,
                 default_audience: Optional[str] = None,
                 from_label: Optional[str] = None):
        self.api_key = api_key
        self.default_sender = default_sender      # from_email
        self.default_audience = default_audience  # list id
        self.from_label = from_label              # display name

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "revision": REVISION,
            "accept": "application/json",
            "content-type": "application/json",
        }

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
        sender = sender or self.default_sender
        audience = audience or self.default_audience
        if not sender:
            return {"error": "No sender (from_email) — set client default or pass sender"}
        if not audience:
            return {"error": "No audience (Klaviyo list id) — set client default or pass audience"}

        try:
            with httpx.Client(timeout=60) as c:
                # 1. template
                tpl = c.post(f"{KLAVIYO_BASE}/templates/", headers=self._headers(), json={
                    "data": {"type": "template", "attributes": {
                        "name": name, "editor_type": "CODE", "html": html}}})
                if tpl.status_code >= 400:
                    return {"error": f"Klaviyo template {tpl.status_code}: {tpl.text[:400]}"}
                template_id = tpl.json()["data"]["id"]

                # 2. campaign (draft) with one email message
                camp_payload = {"data": {"type": "campaign", "attributes": {
                    "name": name,
                    "audiences": {"included": [audience], "excluded": []},
                    "campaign-messages": {"data": [{
                        "type": "campaign-message",
                        "attributes": {"definition": {
                            "channel": "email",
                            "label": name,
                            "content": {
                                "subject": subject,
                                "preview_text": preheader or "",
                                "from_email": sender,
                                "from_label": self.from_label or sender,
                            },
                        }},
                    }]},
                }}}
                camp = c.post(f"{KLAVIYO_BASE}/campaigns/", headers=self._headers(), json=camp_payload)
                if camp.status_code >= 400:
                    return {"error": f"Klaviyo campaign {camp.status_code}: {camp.text[:400]}"}
                cdata = camp.json()["data"]
                campaign_id = cdata["id"]
                # message id lives in relationships
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
                        return {"error": f"Klaviyo assign-template {assign.status_code}: "
                                         f"{assign.text[:300]} (campaign {campaign_id} created)"}

            return {
                "id": campaign_id, "name": name, "subject": subject,
                "status": "draft", "template_id": template_id,
                "href": f"https://www.klaviyo.com/campaign/{campaign_id}/edit",
            }
        except Exception as e:
            return {"error": str(e)}

    # ── lists ───────────────────────────────────────────────────────────────
    def list_senders(self) -> Dict:
        # Klaviyo has no senders endpoint — from_email/from_label are set per message.
        return {"senders": [], "note": "Klaviyo uses from_email/from_label per message; "
                "set them in client defaults (sender = from_email, from_label = display name)."}

    def list_audiences(self) -> Dict:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{KLAVIYO_BASE}/lists/", headers=self._headers())
            r.raise_for_status()
        out = [{"id": l["id"], "name": l.get("attributes", {}).get("name")}
               for l in r.json().get("data", [])]
        return {"audiences": out, "count": len(out)}

    def list_drafts(self, name_filter: Optional[str] = None) -> Dict:
        params = {"filter": "equals(messages.channel,'email')"}
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{KLAVIYO_BASE}/campaigns/", headers=self._headers(), params=params)
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
                    r = c.delete(f"{KLAVIYO_BASE}/campaigns/{cid}/", headers=self._headers())
                    if r.status_code in (200, 204):
                        deleted.append(cid)
                    else:
                        errors[cid] = f"HTTP {r.status_code}: {r.text[:150]}"
                except Exception as e:
                    errors[cid] = str(e)
        return {"deleted": deleted, "errors": errors,
                "success": len(deleted), "failed": len(errors)}
