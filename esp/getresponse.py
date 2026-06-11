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

    # ══════════════════════════════════════════════════════════════════════
    # Automation / marketing API (GetResponse-specific, beyond BaseESP).
    # Readable errors: every call returns either the parsed JSON or {"error": ...}.
    # ══════════════════════════════════════════════════════════════════════

    def _req(self, method: str, path: str, *, params=None, json_body=None,
             timeout: int = 60):
        """Single request. Returns (data, error_str). data is parsed JSON
        (or {} on 202/204). error_str is None on success."""
        url = f"{self.base}{path}"
        headers = dict(self._headers())
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.request(method, url, headers=headers, params=params, json=json_body)
        except Exception as e:
            return None, f"request failed: {e}"
        if r.status_code >= 400:
            # GR returns {"code":..., "codeDescription":..., "message":..., "context":...}
            try:
                body = r.json()
                msg = body.get("message") or body.get("codeDescription") or r.text[:300]
                ctx = body.get("context")
                detail = f"GR {r.status_code}: {msg}"
                if ctx:
                    detail += f" · context={json.dumps(ctx, ensure_ascii=False)[:300]}"
            except Exception:
                detail = f"GR {r.status_code}: {r.text[:300]}"
            return None, detail
        if r.status_code in (202, 204) or not r.content:
            return {}, None
        try:
            return r.json(), None
        except Exception:
            return {}, None

    def _paginate(self, path: str, params: Optional[Dict] = None,
                  max_pages: int = 40) -> List[Dict]:
        """Follow GR per-page pagination (perPage=100) until a short page."""
        out: List[Dict] = []
        page = 1
        base_params = dict(params or {})
        while page <= max_pages:
            p = {**base_params, "page": page, "perPage": 100}
            data, err = self._req("GET", path, params=p)
            if err or not isinstance(data, list):
                break
            out.extend(data)
            if len(data) < 100:
                break
            page += 1
        return out

    # ── newsletters: draft + scheduled broadcast ──────────────────────────
    def create_newsletter_draft(self, name, subject, html, plain="",
                                campaign_id=None, sender=None, preheader=None) -> Dict:
        """Create a GR newsletter DRAFT (editable in the app). Accepts both
        HTML and plain bodies; campaign_id picks the list."""
        sender = sender or self.default_sender
        campaign_id = campaign_id or self.default_audience
        if not sender:
            return {"error": "No sender (fromFieldId) — pass sender or set client default"}
        if not campaign_id:
            return {"error": "No campaignId — pass campaign_id or set client default"}
        if preheader and html:
            span = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
                    f'{preheader}</div>')
            html = html.replace("<table", span + "<table", 1) if "<table" in html else span + html
        payload = {
            "name": name, "type": "draft", "subject": subject,
            "fromField": {"fromFieldId": sender},
            "replyTo": {"fromFieldId": sender},
            "campaign": {"campaignId": campaign_id},
            "content": {"html": html or "", "plain": plain or ""},
            "flags": ["openrate", "clicktrack"],
            "sendSettings": {"selectedCampaigns": [campaign_id]},
        }
        data, err = self._req("POST", "/newsletters", json_body=payload)
        if err:
            return {"error": err}
        return {"newsletterId": data.get("newsletterId") or data.get("id"),
                "name": name, "subject": subject, "type": "draft",
                "href": data.get("href")}

    def schedule_newsletter(self, send_on: str, newsletter_id: Optional[str] = None,
                            name=None, subject=None, html=None, plain="",
                            campaign_id=None, sender=None, preheader=None,
                            segment_ids: Optional[List[str]] = None) -> Dict:
        """Schedule a broadcast for a future time (sendOn, ISO 8601).
        Either reference an existing draft (newsletter_id → its content is
        copied into a scheduled broadcast) OR pass content inline.
        Recipients: campaign_id (list) and/or segment_ids (saved searches)."""
        if not send_on:
            return {"error": "send_on (ISO 8601, e.g. 2026-06-20T09:00:00+0200) is required"}

        if newsletter_id:
            src, err = self._req("GET", f"/newsletters/{newsletter_id}")
            if err:
                return {"error": f"could not load draft {newsletter_id}: {err}"}
            subject = subject or src.get("subject")
            name = name or f"{src.get('name', 'Newsletter')} (scheduled)"
            content = src.get("content") or {}
            html = html if html is not None else content.get("html", "")
            plain = plain or content.get("plain", "")
            sender = sender or (src.get("fromField") or {}).get("fromFieldId")
            campaign_id = campaign_id or (src.get("campaign") or {}).get("campaignId")

        sender = sender or self.default_sender
        campaign_id = campaign_id or self.default_audience
        if not (name and subject):
            return {"error": "name and subject required (pass them or a valid newsletter_id)"}
        if not sender:
            return {"error": "No sender (fromFieldId)"}
        if not (campaign_id or segment_ids):
            return {"error": "No recipients — pass campaign_id and/or segment_ids"}
        if preheader and html:
            span = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
                    f'{preheader}</div>')
            html = html.replace("<table", span + "<table", 1) if "<table" in html else span + html

        send_settings: Dict = {}
        if campaign_id:
            send_settings["selectedCampaigns"] = [campaign_id]
        if segment_ids:
            send_settings["selectedSegments"] = segment_ids
        payload = {
            "name": name, "type": "broadcast", "subject": subject,
            "fromField": {"fromFieldId": sender},
            "replyTo": {"fromFieldId": sender},
            "content": {"html": html or "", "plain": plain or ""},
            "flags": ["openrate", "clicktrack"],
            "sendOn": send_on,
            "sendSettings": send_settings,
        }
        if campaign_id:
            payload["campaign"] = {"campaignId": campaign_id}
        data, err = self._req("POST", "/newsletters", json_body=payload)
        if err:
            return {"error": err}
        return {"newsletterId": data.get("newsletterId") or data.get("id"),
                "name": name, "subject": subject, "type": "broadcast",
                "sendOn": send_on, "href": data.get("href"),
                "copiedFromDraft": newsletter_id}

    # ── segments (search-contacts) ─────────────────────────────────────────
    def get_segments(self) -> Dict:
        items = self._paginate("/search-contacts")
        out = [{"id": s.get("searchContactId"), "name": s.get("name"),
                "createdOn": s.get("createdOn"), "href": s.get("href")}
               for s in items]
        return {"segments": out, "count": len(out)}

    def create_segment(self, name: str, conditions: List[Dict],
                       campaign_ids: Optional[List[str]] = None,
                       subscribers_type: Optional[List[str]] = None,
                       condition_logic: str = "and",
                       section_logic: str = "or",
                       subscriber_cycle: Optional[List[str]] = None,
                       subscription_date: str = "all_time") -> Dict:
        """Create a saved segment (POST /v3/search-contacts).
        conditions: list of {conditionType, operator, value, [operatorType]}.
          e.g. {"conditionType":"tag","operator":"exists","operatorType":"exists","value":"<tagId>"}
               {"conditionType":"email","operator":"not_contains","operatorType":"string_operator","value":"@test.com"}
        """
        if not name:
            return {"error": "name is required"}
        if not conditions:
            return {"error": "at least one condition is required"}
        norm = []
        for cond in conditions:
            if "conditionType" not in cond or "operator" not in cond:
                return {"error": f"each condition needs conditionType + operator (got {cond})"}
            c = {
                "conditionType": cond["conditionType"],
                "operator": cond["operator"],
                "operatorType": cond.get("operatorType", "string_operator"),
            }
            if "value" in cond:
                c["value"] = cond["value"]
            norm.append(c)
        # campaignIdsList is required by GR. Default to ALL lists if not given.
        if not campaign_ids:
            try:
                campaign_ids = [a["id"] for a in self.list_audiences().get("audiences", []) if a.get("id")]
            except Exception:
                campaign_ids = []
            if not campaign_ids:
                return {"error": "campaign_ids required (could not auto-list campaigns)"}
        section = {
            "campaignIdsList": campaign_ids,
            "logicOperator": condition_logic,
            "subscriberCycle": subscriber_cycle or ["receiving_autoresponder", "not_receiving_autoresponder"],
            "subscriptionDate": subscription_date,
            "conditions": norm,
        }
        payload = {
            "name": name,
            "subscribersType": subscribers_type or ["subscribed"],
            "sectionLogicOperator": section_logic,
            "section": [section],
        }
        data, err = self._req("POST", "/search-contacts", json_body=payload)
        if err:
            return {"error": err}
        return {"segmentId": data.get("searchContactId") or data.get("id"),
                "name": name, "href": data.get("href")}

    # ── tags ───────────────────────────────────────────────────────────────
    def get_tags(self) -> List[Dict]:
        items = self._paginate("/tags")
        return [{"tagId": t.get("tagId"), "name": t.get("name")} for t in items]

    def create_tag(self, name: str) -> Dict:
        data, err = self._req("POST", "/tags", json_body={"name": name})
        if err:
            return {"error": err}
        return {"tagId": data.get("tagId"), "name": data.get("name", name)}

    def _resolve_tag_ids(self, tag_names: List[str]) -> Dict:
        """Find-or-create tags by name → return {name: tagId} (+ errors)."""
        existing = {t["name"].lower(): t["tagId"] for t in self.get_tags()}
        ids, errors = {}, {}
        for nm in tag_names:
            tid = existing.get(nm.lower())
            if not tid:
                created = self.create_tag(nm)
                if "error" in created:
                    errors[nm] = created["error"]
                    continue
                tid = created["tagId"]
                existing[nm.lower()] = tid
            ids[nm] = tid
        return {"ids": ids, "errors": errors}

    # ── custom fields ────────────────────────────────────────────────────
    def get_custom_fields(self) -> List[Dict]:
        items = self._paginate("/custom-fields")
        return [{"customFieldId": f.get("customFieldId"), "name": f.get("name"),
                 "type": f.get("type"), "fieldType": f.get("fieldType")} for f in items]

    def create_custom_field(self, name: str, field_type: str = "text",
                            values: Optional[List[str]] = None, hidden: bool = False) -> Dict:
        payload = {"name": name, "type": field_type,
                   "hidden": "true" if hidden else "false", "values": values or []}
        data, err = self._req("POST", "/custom-fields", json_body=payload)
        if err:
            return {"error": err}
        return {"customFieldId": data.get("customFieldId"), "name": data.get("name", name)}

    def _resolve_custom_fields(self, fields: Dict[str, object]) -> Dict:
        """Map {name: value} → [{customFieldId, value:[...]}], creating missing
        fields as text. Returns {values: [...], errors: {...}}."""
        existing = {f["name"].lower(): f["customFieldId"] for f in self.get_custom_fields()}
        values, errors = [], {}
        for name, val in fields.items():
            cid = existing.get(name.lower())
            if not cid:
                created = self.create_custom_field(name)
                if "error" in created:
                    errors[name] = created["error"]
                    continue
                cid = created["customFieldId"]
                existing[name.lower()] = cid
            vlist = val if isinstance(val, list) else [str(val)]
            values.append({"customFieldId": cid, "value": vlist})
        return {"values": values, "errors": errors}

    # ── contacts ───────────────────────────────────────────────────────────
    def find_contact(self, email: str, campaign_id: Optional[str] = None) -> Optional[Dict]:
        params = {"query[email]": email}
        if campaign_id:
            params["query[campaignId]"] = campaign_id
        data, err = self._req("GET", "/contacts", params=params)
        if err or not isinstance(data, list) or not data:
            return None
        return data[0]

    def assign_tags_to_contact(self, contact_id: str, tag_ids: List[str]) -> Optional[str]:
        """Append tags (does NOT replace existing). Returns error str or None."""
        body = {"tags": [{"tagId": t} for t in tag_ids]}
        _data, err = self._req("POST", f"/contacts/{contact_id}/tags", json_body=body)
        return err

    def set_contact_custom_fields(self, contact_id: str,
                                  cf_values: List[Dict]) -> Optional[str]:
        """Merge custom fields onto a contact (keeps fields not mentioned).
        Returns error str or None."""
        cur, err = self._req("GET", f"/contacts/{contact_id}",
                             params={"fields": "customFieldValues"})
        merged = {}
        if not err and isinstance(cur, dict):
            for f in cur.get("customFieldValues", []) or []:
                merged[f.get("customFieldId")] = f.get("value")
        for f in cf_values:
            merged[f["customFieldId"]] = f["value"]
        body = {"customFieldValues": [{"customFieldId": k, "value": v}
                                       for k, v in merged.items()]}
        _data, err = self._req("POST", f"/contacts/{contact_id}", json_body=body)
        return err

    def upsert_contact(self, email: str, campaign_id: Optional[str] = None,
                       name: Optional[str] = None,
                       tags: Optional[List[str]] = None,
                       custom_fields: Optional[Dict[str, object]] = None,
                       day_of_cycle: Optional[str] = None) -> Dict:
        """Add or update a contact. tags = list of NAMES (find-or-create).
        custom_fields = {name: value}. Returns a summary dict."""
        campaign_id = campaign_id or self.default_audience
        if not email:
            return {"error": "email is required"}
        if not campaign_id:
            return {"error": "campaign_id required (or set client default)"}

        tag_ids, tag_errors = [], {}
        if tags:
            res = self._resolve_tag_ids(tags)
            tag_ids = list(res["ids"].values())
            tag_errors = res["errors"]

        cf_values, cf_errors = [], {}
        if custom_fields:
            res = self._resolve_custom_fields(custom_fields)
            cf_values = res["values"]
            cf_errors = res["errors"]

        existing = self.find_contact(email, campaign_id)
        if existing:
            cid = existing.get("contactId")
            updated = []
            if name and name != existing.get("name"):
                _d, err = self._req("POST", f"/contacts/{cid}", json_body={"name": name})
                if err:
                    return {"error": f"update name: {err}"}
                updated.append("name")
            if tag_ids:
                err = self.assign_tags_to_contact(cid, tag_ids)
                if err:
                    return {"error": f"assign tags: {err}", "contactId": cid}
                updated.append("tags")
            if cf_values:
                err = self.set_contact_custom_fields(cid, cf_values)
                if err:
                    return {"error": f"custom fields: {err}", "contactId": cid}
                updated.append("customFields")
            return {"action": "updated", "contactId": cid, "email": email,
                    "updated": updated, "tagErrors": tag_errors, "customFieldErrors": cf_errors}

        # create new
        payload: Dict = {"email": email, "campaign": {"campaignId": campaign_id}}
        if name:
            payload["name"] = name
        if day_of_cycle is not None:
            payload["dayOfCycle"] = str(day_of_cycle)
        if tag_ids:
            payload["tags"] = tag_ids
        if cf_values:
            payload["customFieldValues"] = cf_values
        _d, err = self._req("POST", "/contacts", json_body=payload)
        if err:
            return {"error": err, "tagErrors": tag_errors, "customFieldErrors": cf_errors}
        return {"action": "created", "email": email, "campaignId": campaign_id,
                "queued": True, "tagErrors": tag_errors, "customFieldErrors": cf_errors}

    def trigger_automation_event(self, email: str, campaign_id: Optional[str] = None,
                                 add_tags: Optional[List[str]] = None,
                                 set_custom_fields: Optional[Dict[str, object]] = None) -> Dict:
        """Fire GR Automation triggers by adding tags and/or changing custom
        fields on a contact (resolved by email). Contact must already exist."""
        campaign_id = campaign_id or self.default_audience
        if not email:
            return {"error": "email is required"}
        if not (add_tags or set_custom_fields):
            return {"error": "nothing to do — pass add_tags and/or set_custom_fields"}
        contact = self.find_contact(email, campaign_id)
        if not contact:
            return {"error": f"contact '{email}' not found"
                              + (f" in campaign {campaign_id}" if campaign_id else "")
                              + " — use upsert_contact first"}
        cid = contact.get("contactId")
        applied, errors = [], {}
        if add_tags:
            res = self._resolve_tag_ids(add_tags)
            errors.update({f"tag:{k}": v for k, v in res["errors"].items()})
            ids = list(res["ids"].values())
            if ids:
                err = self.assign_tags_to_contact(cid, ids)
                if err:
                    errors["assign_tags"] = err
                else:
                    applied.append(f"tags:{list(res['ids'].keys())}")
        if set_custom_fields:
            res = self._resolve_custom_fields(set_custom_fields)
            errors.update({f"cf:{k}": v for k, v in res["errors"].items()})
            if res["values"]:
                err = self.set_contact_custom_fields(cid, res["values"])
                if err:
                    errors["set_custom_fields"] = err
                else:
                    applied.append(f"customFields:{list(set_custom_fields.keys())}")
        return {"contactId": cid, "email": email, "applied": applied,
                "errors": errors, "ok": not errors}

    # ── statistics ─────────────────────────────────────────────────────────
    def get_campaign_statistics(self, newsletter_ids: Optional[List[str]] = None,
                                campaign_ids: Optional[List[str]] = None,
                                group_by: str = "total",
                                date_from: Optional[str] = None,
                                date_to: Optional[str] = None) -> Dict:
        """Newsletter delivery stats (opens, clicks, unsubscribes, bounces).
        group_by: total | hour | day | month. GR requires at least one of
        newsletter_ids or campaign_ids."""
        if not newsletter_ids and not campaign_ids:
            return {"error": "Provide newsletter_ids (specific sends) or campaign_ids "
                             "(all sends to a list). GR rejects an unfiltered stats query."}
        params: Dict = {"query[groupBy]": group_by}
        if newsletter_ids:
            params["query[newsletterId]"] = ",".join(newsletter_ids)
        if campaign_ids:
            params["query[campaignId]"] = ",".join(campaign_ids)
        if date_from:
            params["query[createdOn][from]"] = date_from
        if date_to:
            params["query[createdOn][to]"] = date_to
        data, err = self._req("GET", "/newsletters/statistics", params=params)
        if err:
            return {"error": err}
        rows = data if isinstance(data, list) else [data]
        def num(x):
            try:
                return int(x)
            except (TypeError, ValueError):
                return x
        out = []
        for s in rows:
            out.append({
                "newsletterId": s.get("newsletterId"),
                "timeInterval": s.get("timeInterval"),
                "sent": num(s.get("sent", 0)),
                "delivered": num(s.get("delivered", 0)),
                "opened": num(s.get("opened", 0)),
                "uniqueOpened": num(s.get("uniqueOpened", 0)),
                "clicked": num(s.get("clicked", 0)),
                "uniqueClicked": num(s.get("uniqueClicked", 0)),
                "bounced": num(s.get("bounced", 0)),
                "unsubscribed": num(s.get("unsubscribed", 0)),
                "complaints": num(s.get("complaints", 0)),
            })
        return {"groupBy": group_by, "statistics": out, "count": len(out)}
