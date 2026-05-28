"""GetResponse ESP implementation."""
import json
import base64
import logging
from typing import Dict, List, Optional

import httpx

from .base import BaseESP

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.getresponse.com/v3"


class GetResponseESP(BaseESP):
    def __init__(self, api_key: str, base: str = DEFAULT_BASE,
                 default_sender: Optional[str] = None,
                 default_audience: Optional[str] = None):
        self.api_key = api_key
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.default_sender = default_sender
        self.default_audience = default_audience

    def _headers(self) -> Dict[str, str]:
        return {"X-Auth-Token": f"api-key {self.api_key}"}

    # ── images ──────────────────────────────────────────────────────────────
    def upload_image(self, image_bytes: bytes, filename: str, content_type: str = "") -> str:
        name, _, ext = filename.rpartition(".")
        if not name:
            name, ext = filename, "png"
        payload = {
            "name": name,
            "extension": ext,
            "content": base64.b64encode(image_bytes).decode("ascii"),
            "folder": None,
        }
        with httpx.Client(timeout=120) as c:
            r = c.post(f"{self.base}/file-library/files",
                       headers={**self._headers(), "Content-Type": "application/json"},
                       json=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"GR file upload {r.status_code}: {r.text[:400]}")
            data = r.json()
        cdn_url = (data.get("url") or data.get("publicUrl") or
                   data.get("fileUrl") or data.get("src") or
                   (data.get("file") or {}).get("url"))
        if not cdn_url:
            raise RuntimeError(f"GR upload ok but no URL: {json.dumps(data)[:300]}")
        return cdn_url

    # ── draft ───────────────────────────────────────────────────────────────
    def create_draft(self, name, subject, html, preheader=None, sender=None, audience=None) -> Dict:
        sender = sender or self.default_sender
        audience = audience or self.default_audience
        if not sender:
            return {"error": "No sender (fromFieldId) — set client default or pass sender"}
        if not audience:
            return {"error": "No audience (campaignId) — set client default or pass audience"}

        if preheader:
            span = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
                    f'{preheader}</div>')
            html = html.replace("<table", span + "<table", 1) if "<table" in html else span + html

        payload = {
            "name": name,
            "type": "draft",
            "subject": subject,
            "fromField": {"fromFieldId": sender},
            "replyTo": {"fromFieldId": sender},
            "campaign": {"campaignId": audience},
            "content": {"html": html, "plain": ""},
            "flags": ["openrate", "clicktrack"],
            "sendSettings": {"selectedCampaigns": [audience]},
        }
        try:
            with httpx.Client(timeout=60) as c:
                r = c.post(f"{self.base}/newsletters",
                           headers={**self._headers(), "Content-Type": "application/json"},
                           json=payload)
                r.raise_for_status()
            d = r.json()
            return {
                "id": d.get("newsletterId") or d.get("id"),
                "name": name, "subject": subject,
                "status": d.get("status", "draft"),
                "href": d.get("href"),
            }
        except httpx.HTTPStatusError as e:
            return {"error": f"GR API {e.response.status_code}: {e.response.text[:400]}"}

    # ── lists ───────────────────────────────────────────────────────────────
    def list_senders(self) -> Dict:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{self.base}/from-fields", headers=self._headers())
            r.raise_for_status()
        fields = [{"id": f.get("fromFieldId"), "email": f.get("email"),
                   "name": f.get("name"), "isDefault": f.get("isDefault")}
                  for f in r.json()]
        return {"senders": fields, "count": len(fields)}

    def list_audiences(self) -> Dict:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{self.base}/campaigns", headers=self._headers(),
                      params={"page": 1, "perPage": 100})
            r.raise_for_status()
        camps = r.json()
        if isinstance(camps, dict):
            camps = camps.get("campaigns", [])
        out = [{"id": c.get("campaignId"), "name": c.get("name")} for c in camps]
        return {"audiences": out, "count": len(out)}

    def list_drafts(self, name_filter: Optional[str] = None) -> Dict:
        params = {"page": 1, "perPage": 100, "query[type]": "draft"}
        if name_filter:
            params["query[name]"] = name_filter
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{self.base}/newsletters", headers=self._headers(), params=params)
            r.raise_for_status()
        out = [{"id": d.get("newsletterId"), "name": d.get("name"),
                "subject": d.get("subject")} for d in r.json()]
        return {"drafts": out, "count": len(out)}

    def delete_drafts(self, ids: List[str]) -> Dict:
        deleted, errors = [], {}
        with httpx.Client(timeout=30) as c:
            for nid in ids:
                try:
                    r = c.delete(f"{self.base}/newsletters/{nid}", headers=self._headers())
                    if r.status_code in (200, 204):
                        deleted.append(nid)
                    else:
                        errors[nid] = f"HTTP {r.status_code}: {r.text[:150]}"
                except Exception as e:
                    errors[nid] = str(e)
        return {"deleted": deleted, "errors": errors,
                "success": len(deleted), "failed": len(errors)}
