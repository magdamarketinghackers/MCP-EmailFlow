import logging
import os
import json
import traceback
import contextlib
from typing import Dict, Any, List, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

import store
import admin
from esp import get_esp, SUPPORTED
from esp.getresponse import GetResponseESP
from esp.images import fetch_and_prepare

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """
This server creates email DRAFTS from Figma designs, for MULTIPLE clients.
Each client can have MULTIPLE ESP connections (e.g. one for GetResponse and one
for Klaviyo during a migration). Managed in the /admin panel; you never see raw
API keys, only slugs and connection labels.

══════════════════════════════════════════════════════════════════════════
WORKFLOW
══════════════════════════════════════════════════════════════════════════
0. PICK THE CLIENT (+ optional connection)
   • Call list_clients — returns slug + connections [{label, esp, is_primary}].
   • Ask the user which client if not specified.
   • If a client has MORE THAN ONE connection (e.g. 'getresponse' + 'klaviyo'),
     ASK which one to use. Otherwise omit `connection` — the primary one is used.
   • Every other tool takes `client` (required) and `connection` (optional label).

1. ASK USER — these THREE inputs are REQUIRED for every draft (never guess them):
   • Figma URL or node-id (must contain fileKey + nodeId)
   • Email **subject** (max 128 chars) — the line shown in the inbox
   • **Preheader** / preview text (1-2 sentences shown next to subject)
   ALWAYS ask for subject and preheader explicitly before calling create_draft,
   even if you can guess them from the design copy. The user must approve them.
   Sender + audience default to the client config — only ask if user wants to override.

2. FETCH DESIGN via Figma MCP (OAuth — uses the user's own Figma account):
   Call mcp__claude_ai_Figma__get_design_context with fileKey + nodeId.
   If it returns "file not publicly available" / access denied → the user's Figma
   account lacks access: they must connect Figma in claude.ai → Settings → Integrations,
   and be a member of the team/project that owns the file. This server has NO
   server-side Figma access by design.

3. UPLOAD IMAGES:
   Call bulk_upload_urls(client, items=[{url, name}, ...]).
   Returns hosted CDN URLs (GR File Library or Klaviyo images). SVG auto-converted to PNG.

4. GENERATE HTML matching the Figma design 1:1:
   • Width 640px, font Jost. Table-based. NO <!DOCTYPE>/<html>/<head>/<body>.
   • Font-weight: Figma 500 (Medium) → CSS 700 (Bold). 400 stays 400; 600/700 unchanged.
   • UPPERCASE text: write literal CAPS in HTML, never CSS text-transform:uppercase
     (several clients incl. some Outlook ignore it).
   • Photos: fixed width+height attrs + style="object-fit:cover;object-position:center;".
   • Buttons: full email width minus 8px horizontal padding on the outer cell.
     Background = exact hex from Figma.
   • DO NOT add {unsubscribe}/[UNSUBSCRIBE]/unsubscribe link — the ESP appends it.
   • target="_blank" on all anchors; wrap links from the Figma node (logo→home,
     CTA→collection, product photos→product pages, social→profiles).

   IVERESSE FOOTER PATTERN (apply for iveresse unless the node clearly diverges):
   • Two info icons in a row: "Szyjemy w Polsce" + "Bezpłatna dostawa",
     icon LEFT, two text lines RIGHT. 8px horizontal padding on the row.
   • Divider line BELOW the icons (never above).
   • Footer links: Polityka prywatności · Regulamin · Kontakt (centered, 14px, underlined).
   • Social: TikTok · Instagram · Facebook (24px icons, 24px gap).
   • © 2026 Iveresse, All Rights Reserved (11px Jost light, centered).

5. CREATE DRAFT:
   Call create_draft(client, name, subject, html, preheader).
   sender/audience optional — client defaults apply.

══════════════════════════════════════════════════════════════════════════
TOOLS (all except list_clients take `client`)
══════════════════════════════════════════════════════════════════════════
Figma → draft flow (any ESP):
• list_clients · bulk_upload_urls · create_draft
• list_senders · list_audiences · list_drafts · delete_drafts

GetResponse automation (GR connections only):
• create_newsletter_draft — draft with HTML/plain + campaign_id
• schedule_newsletter — schedule a broadcast for a future time (sendOn);
  from an existing draft (newsletter_id) or inline; to lists and/or segments
• get_segments / create_segment — saved searches (search-contacts);
  conditions like tag-exists, email-not-contains, etc.
• upsert_contact — add/update a contact (tags by name, custom fields by name)
• trigger_automation_event — add tags / set custom fields on a contact to
  fire GR Automation workflows
• get_campaign_statistics — opens, clicks, unsubscribes, bounces per newsletter

GR automation playbook:
  upsert_contact → (optional) create_segment → create_newsletter_draft →
  schedule_newsletter → get_campaign_statistics. Tags/fields set via
  trigger_automation_event drive GR Automation flows.
"""

server = Server("email-flow", instructions=SERVER_INSTRUCTIONS)
session_manager = StreamableHTTPSessionManager(app=server, stateless=True)


# ── client resolution ───────────────────────────────────────────────────────

def _resolve(slug: str, connection: Optional[str] = None):
    """Return (esp, info_dict, None) or (None, None, error_dict).
       info_dict carries which client/connection was used (for logging/output)."""
    if not slug:
        return None, None, {"error": "Missing 'client'. Call list_clients first."}
    if not store.available():
        return None, None, {"error": "Client store not configured (DATABASE_URL / MASTER_KEY missing)."}
    conn = store.get_connection(slug, connection)
    if not conn:
        labels = store.list_connection_labels(slug)
        if not labels:
            avail = [c["slug"] for c in store.list_clients()]
            return None, None, {"error": f"Unknown client '{slug}'. Available: {avail}"}
        return None, None, {"error": f"Unknown connection '{connection}' for client '{slug}'. "
                                       f"Available: {labels}"}
    try:
        return get_esp(conn), {"client": slug, "connection": conn["label"]}, None
    except Exception as e:
        return None, None, {"error": f"Failed to init ESP for '{slug}/{conn['label']}': {e}"}


def _resolve_gr(slug: str, connection: Optional[str] = None):
    """Like _resolve but requires a GetResponse connection (automation tools
    are GR-specific). Returns (gr_esp, info, None) or (None, None, error)."""
    esp, info, err = _resolve(slug, connection)
    if err:
        return None, None, err
    if not isinstance(esp, GetResponseESP):
        return None, None, {"error": f"This tool is GetResponse-only; "
                                      f"'{slug}/{info['connection']}' is not a GetResponse connection."}
    return esp, info, None


# ── tool implementations ──────────────────────────────────────────────────────

def _list_clients() -> Dict:
    out = []
    for c in store.list_clients():
        out.append({
            "slug": c["slug"],
            "name": c["name"],
            "connections": [
                {"label": cn["label"], "esp": cn["esp_type"], "is_primary": cn["is_primary"]}
                for cn in c["connections"]
            ],
        })
    return {"clients": out, "count": len(out)}


def _bulk_upload_urls(client: str, items: List[Dict],
                      connection: Optional[str] = None) -> Dict:
    esp, info, err = _resolve(client, connection)
    if err:
        return err
    uploaded, errors = {}, {}
    for item in items:
        name = item["name"]
        try:
            img, ext, ct = fetch_and_prepare(item["url"])
            url = esp.upload_image(img, f"{name}.{ext}", content_type=ct)
            uploaded[name] = url
            logger.info(f"[{client}/{info['connection']}] ✓ {name} → {url}")
        except Exception as e:
            errors[name] = str(e)
            logger.error(f"[{client}/{info['connection']}] ✗ {name}: {e}")
    return {"client": client, "connection": info["connection"],
            "uploaded": uploaded, "errors": errors, "total": len(items),
            "success": len(uploaded), "failed": len(errors)}


def _create_draft(client: str, name: str, subject: str, html: str,
                  preheader: Optional[str] = None,
                  sender: Optional[str] = None,
                  audience: Optional[str] = None,
                  connection: Optional[str] = None) -> Dict:
    if not (subject and 2 <= len(subject) <= 128):
        return {"error": f"subject must be 2-128 chars (got {len(subject) if subject else 0})"}
    if not (name and 2 <= len(name) <= 128):
        return {"error": f"name must be 2-128 chars (got {len(name) if name else 0})"}
    if not html or len(html.strip()) < 10:
        return {"error": "html is empty or too short"}
    esp, info, err = _resolve(client, connection)
    if err:
        return err
    result = esp.create_draft(name, subject, html, preheader=preheader,
                              sender=sender, audience=audience)
    if isinstance(result, dict):
        result.setdefault("connection", info["connection"])
    return result


def _list_senders(client: str, connection: Optional[str] = None) -> Dict:
    esp, _info, err = _resolve(client, connection)
    if err:
        return err
    try:
        return esp.list_senders()
    except Exception as e:
        return {"error": str(e)}


def _list_audiences(client: str, connection: Optional[str] = None) -> Dict:
    esp, _info, err = _resolve(client, connection)
    if err:
        return err
    try:
        return esp.list_audiences()
    except Exception as e:
        return {"error": str(e)}


def _list_drafts(client: str, name_filter: Optional[str] = None,
                 connection: Optional[str] = None) -> Dict:
    esp, _info, err = _resolve(client, connection)
    if err:
        return err
    try:
        return esp.list_drafts(name_filter=name_filter)
    except Exception as e:
        return {"error": str(e)}


def _delete_drafts(client: str, ids: List[str],
                   connection: Optional[str] = None) -> Dict:
    esp, _info, err = _resolve(client, connection)
    if err:
        return err
    try:
        return esp.delete_drafts(ids)
    except Exception as e:
        return {"error": str(e)}


# ── GetResponse automation tools (GR-only) ──────────────────────────────────

def _gr_call(client, connection, fn):
    esp, _info, err = _resolve_gr(client, connection)
    if err:
        return err
    try:
        return fn(esp)
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def _create_newsletter_draft(client, name, subject, html=None, plain="",
                             campaign_id=None, sender=None, preheader=None,
                             connection=None) -> Dict:
    if not (subject and 2 <= len(subject) <= 128):
        return {"error": "subject must be 2-128 chars"}
    if not (name and 2 <= len(name) <= 128):
        return {"error": "name must be 2-128 chars"}
    if not ((html and html.strip()) or (plain and plain.strip())):
        return {"error": "provide html and/or plain body"}
    return _gr_call(client, connection, lambda e: e.create_newsletter_draft(
        name, subject, html or "", plain=plain or "", campaign_id=campaign_id,
        sender=sender, preheader=preheader))


def _schedule_newsletter(client, send_on, newsletter_id=None, name=None, subject=None,
                         html=None, plain="", campaign_id=None, sender=None,
                         preheader=None, segment_ids=None, connection=None) -> Dict:
    return _gr_call(client, connection, lambda e: e.schedule_newsletter(
        send_on, newsletter_id=newsletter_id, name=name, subject=subject,
        html=html, plain=plain or "", campaign_id=campaign_id, sender=sender,
        preheader=preheader, segment_ids=segment_ids))


def _get_segments(client, connection=None) -> Dict:
    return _gr_call(client, connection, lambda e: e.get_segments())


def _create_segment(client, name, conditions, campaign_ids=None, subscribers_type=None,
                    condition_logic="and", section_logic="or",
                    subscriber_cycle=None, subscription_date="all_time",
                    connection=None) -> Dict:
    return _gr_call(client, connection, lambda e: e.create_segment(
        name, conditions, campaign_ids=campaign_ids, subscribers_type=subscribers_type,
        condition_logic=condition_logic, section_logic=section_logic,
        subscriber_cycle=subscriber_cycle, subscription_date=subscription_date))


def _upsert_contact(client, email, campaign_id=None, name=None, tags=None,
                    custom_fields=None, day_of_cycle=None, connection=None) -> Dict:
    return _gr_call(client, connection, lambda e: e.upsert_contact(
        email, campaign_id=campaign_id, name=name, tags=tags,
        custom_fields=custom_fields, day_of_cycle=day_of_cycle))


def _trigger_automation_event(client, email, campaign_id=None, add_tags=None,
                              set_custom_fields=None, connection=None) -> Dict:
    return _gr_call(client, connection, lambda e: e.trigger_automation_event(
        email, campaign_id=campaign_id, add_tags=add_tags,
        set_custom_fields=set_custom_fields))


def _get_campaign_statistics(client, newsletter_ids=None, campaign_ids=None,
                             group_by="total", date_from=None, date_to=None,
                             connection=None) -> Dict:
    return _gr_call(client, connection, lambda e: e.get_campaign_statistics(
        newsletter_ids=newsletter_ids, campaign_ids=campaign_ids, group_by=group_by,
        date_from=date_from, date_to=date_to))


# ── MCP tool registry ─────────────────────────────────────────────────────────

_CLIENT_PROP = {"type": "string", "description": "Client slug (from list_clients), e.g. 'iveresse'"}
_CONN_PROP = {"type": "string",
              "description": "Optional connection label (e.g. 'getresponse', 'klaviyo'). "
                             "Omit to use the client's primary connection."}

ALL_TOOLS = [
    Tool(
        name="list_clients",
        description="List configured clients (slug, name, ESP). Call first to know which client to use.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="bulk_upload_urls",
        description=("Download images from URLs and upload to the client's ESP (GR File Library "
                     "or Klaviyo images). Pass Figma asset URLs from get_design_context. "
                     "SVG auto-converted to PNG. Returns name → hosted CDN URL."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "connection": _CONN_PROP,
                "items": {
                    "type": "array",
                    "description": "List of {url, name} pairs",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "Public image URL"},
                            "name": {"type": "string", "description": "Output filename (no extension)"},
                        },
                        "required": ["url", "name"],
                    },
                },
            },
            "required": ["client", "items"],
        },
    ),
    Tool(
        name="create_draft",
        description=("Create an email draft for the client (GetResponse newsletter draft or "
                     "Klaviyo campaign draft). sender/audience default to the client config."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP,
                "connection": _CONN_PROP,
                "name": {"type": "string", "description": "Internal draft name (2-128 chars)"},
                "subject": {"type": "string", "description": "Subject line (2-128 chars)"},
                "html": {"type": "string", "description": "Full email HTML"},
                "preheader": {"type": "string", "description": "Preview text (recommended)"},
                "sender": {"type": "string", "description": "Optional. Override default sender."},
                "audience": {"type": "string", "description": "Optional. Override default list/audience."},
            },
            "required": ["client", "name", "subject", "html"],
        },
    ),
    Tool(
        name="list_senders",
        description="List available sender identities for the client's ESP.",
        inputSchema={"type": "object",
                     "properties": {"client": _CLIENT_PROP, "connection": _CONN_PROP},
                     "required": ["client"]},
    ),
    Tool(
        name="list_audiences",
        description="List available lists/audiences for the client's ESP.",
        inputSchema={"type": "object",
                     "properties": {"client": _CLIENT_PROP, "connection": _CONN_PROP},
                     "required": ["client"]},
    ),
    Tool(
        name="list_drafts",
        description="List existing drafts for the client. Optional name_filter.",
        inputSchema={
            "type": "object",
            "properties": {"client": _CLIENT_PROP, "connection": _CONN_PROP,
                           "name_filter": {"type": "string", "description": "Filter by name substring"}},
            "required": ["client"],
        },
    ),
    Tool(
        name="delete_drafts",
        description="Delete drafts by id for the client.",
        inputSchema={
            "type": "object",
            "properties": {"client": _CLIENT_PROP, "connection": _CONN_PROP,
                           "ids": {"type": "array", "items": {"type": "string"},
                                   "description": "Draft IDs to delete"}},
            "required": ["client", "ids"],
        },
    ),

    # ── GetResponse automation / marketing (GR connections only) ────────────
    Tool(
        name="create_newsletter_draft",
        description=("GetResponse only. Create an editable newsletter DRAFT with HTML "
                     "and/or plain body, assigned to a list (campaign_id). Returns "
                     "newsletterId for later scheduling. Use create_draft instead if "
                     "you came from the Figma flow."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP, "connection": _CONN_PROP,
                "name": {"type": "string", "description": "Internal draft name (2-128 chars)"},
                "subject": {"type": "string", "description": "Inbox subject (2-128 chars)"},
                "html": {"type": "string", "description": "HTML body (optional if plain given)"},
                "plain": {"type": "string", "description": "Plain-text body (optional)"},
                "campaign_id": {"type": "string", "description": "List/campaign ID. Defaults to client config."},
                "sender": {"type": "string", "description": "fromFieldId. Defaults to client config."},
                "preheader": {"type": "string", "description": "Preview text (recommended)"},
            },
            "required": ["client", "name", "subject"],
        },
    ),
    Tool(
        name="schedule_newsletter",
        description=("GetResponse only. Schedule a broadcast for a future time (send_on, "
                     "ISO 8601 with timezone). Either pass an existing draft's newsletter_id "
                     "(its content is copied into the scheduled broadcast) OR pass content "
                     "inline. Recipients via campaign_id and/or segment_ids (saved searches)."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP, "connection": _CONN_PROP,
                "send_on": {"type": "string", "description": "ISO 8601, e.g. 2026-06-20T09:00:00+0200"},
                "newsletter_id": {"type": "string", "description": "Draft to schedule (copies its content)"},
                "name": {"type": "string", "description": "Override broadcast name"},
                "subject": {"type": "string", "description": "Override subject"},
                "html": {"type": "string", "description": "Inline HTML (if not using a draft)"},
                "plain": {"type": "string", "description": "Inline plain body"},
                "campaign_id": {"type": "string", "description": "Recipient list ID"},
                "segment_ids": {"type": "array", "items": {"type": "string"},
                                "description": "Recipient segment IDs (from get_segments)"},
                "sender": {"type": "string", "description": "fromFieldId override"},
                "preheader": {"type": "string", "description": "Preview text"},
            },
            "required": ["client", "send_on"],
        },
    ),
    Tool(
        name="get_segments",
        description="GetResponse only. List saved segments (search-contacts) with IDs for use in schedule_newsletter.",
        inputSchema={"type": "object",
                     "properties": {"client": _CLIENT_PROP, "connection": _CONN_PROP},
                     "required": ["client"]},
    ),
    Tool(
        name="create_segment",
        description=("GetResponse only. Create a saved segment (POST /v3/search-contacts). "
                     "conditions is a list of {conditionType, operator, value, operatorType}. "
                     "Examples: tag exists → {\"conditionType\":\"tag\",\"operator\":\"exists\","
                     "\"operatorType\":\"exists\",\"value\":\"<tagId>\"}; email not_contains → "
                     "{\"conditionType\":\"email\",\"operator\":\"not_contains\","
                     "\"operatorType\":\"string_operator\",\"value\":\"@x.com\"}. "
                     "condition_logic combines conditions (and/or)."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP, "connection": _CONN_PROP,
                "name": {"type": "string", "description": "Segment name (1-128 chars)"},
                "conditions": {
                    "type": "array",
                    "description": "Condition objects",
                    "items": {
                        "type": "object",
                        "properties": {
                            "conditionType": {"type": "string", "description": "e.g. tag, email, name, geo"},
                            "operator": {"type": "string", "description": "e.g. exists, is, is_not, contains, not_contains"},
                            "operatorType": {"type": "string", "description": "e.g. exists, string_operator"},
                            "value": {"description": "Value (e.g. tagId or string). Omit for exists."},
                        },
                        "required": ["conditionType", "operator"],
                    },
                },
                "campaign_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "Limit to these list IDs (optional)"},
                "condition_logic": {"type": "string", "description": "'and' (default) or 'or'"},
                "subscribers_type": {"type": "array", "items": {"type": "string"},
                                     "description": "Default ['subscribed']"},
            },
            "required": ["client", "name", "conditions"],
        },
    ),
    Tool(
        name="upsert_contact",
        description=("GetResponse only. Add or update a contact by email within a list. "
                     "tags = list of tag NAMES (created if missing). custom_fields = "
                     "{fieldName: value} (created as text if missing). Existing tags/fields "
                     "are merged, not wiped."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP, "connection": _CONN_PROP,
                "email": {"type": "string"},
                "campaign_id": {"type": "string", "description": "List ID. Defaults to client config."},
                "name": {"type": "string", "description": "Contact full name (optional)"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tag names"},
                "custom_fields": {"type": "object", "description": "{fieldName: value}"},
                "day_of_cycle": {"type": "string", "description": "Autoresponder day (optional)"},
            },
            "required": ["client", "email"],
        },
    ),
    Tool(
        name="trigger_automation_event",
        description=("GetResponse only. Fire GR Automation triggers on an existing contact "
                     "(by email) by adding tags and/or changing custom fields. Use this to "
                     "kick off automation workflows that listen for a tag/field change."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP, "connection": _CONN_PROP,
                "email": {"type": "string"},
                "campaign_id": {"type": "string", "description": "Narrow lookup to a list (optional)"},
                "add_tags": {"type": "array", "items": {"type": "string"},
                             "description": "Tag names to add (created if missing)"},
                "set_custom_fields": {"type": "object", "description": "{fieldName: value} to set"},
            },
            "required": ["client", "email"],
        },
    ),
    Tool(
        name="get_campaign_statistics",
        description=("GetResponse only. Delivery stats for newsletters: sent, delivered, "
                     "opened, clicked, bounced, unsubscribed, complaints. Filter by "
                     "newsletter_ids and date range; group_by total|hour|day|month."),
        inputSchema={
            "type": "object",
            "properties": {
                "client": _CLIENT_PROP, "connection": _CONN_PROP,
                "newsletter_ids": {"type": "array", "items": {"type": "string"},
                                   "description": "Filter to these newsletter IDs (specific sends)"},
                "campaign_ids": {"type": "array", "items": {"type": "string"},
                                 "description": "Filter to all sends to these list IDs. Provide newsletter_ids OR campaign_ids."},
                "group_by": {"type": "string", "description": "total (default) | hour | day | month"},
                "date_from": {"type": "string", "description": "ISO date lower bound (createdOn)"},
                "date_to": {"type": "string", "description": "ISO date upper bound (createdOn)"},
            },
            "required": ["client"],
        },
    ),
]


@server.list_tools()
async def list_tools():
    return ALL_TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        result = _dispatch(name, arguments)
    except Exception as e:
        result = {"error": str(e), "traceback": traceback.format_exc()}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


def _dispatch(name: str, args: dict) -> Dict[str, Any]:
    if name == "list_clients":
        return _list_clients()
    if name == "bulk_upload_urls":
        return _bulk_upload_urls(args["client"], args["items"], connection=args.get("connection"))
    if name == "create_draft":
        return _create_draft(args["client"], args["name"], args["subject"], args["html"],
                             preheader=args.get("preheader"), sender=args.get("sender"),
                             audience=args.get("audience"), connection=args.get("connection"))
    if name == "list_senders":
        return _list_senders(args["client"], connection=args.get("connection"))
    if name == "list_audiences":
        return _list_audiences(args["client"], connection=args.get("connection"))
    if name == "list_drafts":
        return _list_drafts(args["client"], args.get("name_filter"),
                            connection=args.get("connection"))
    if name == "delete_drafts":
        return _delete_drafts(args["client"], args["ids"], connection=args.get("connection"))
    # GetResponse automation tools
    if name == "create_newsletter_draft":
        return _create_newsletter_draft(
            args["client"], args["name"], args["subject"], html=args.get("html"),
            plain=args.get("plain", ""), campaign_id=args.get("campaign_id"),
            sender=args.get("sender"), preheader=args.get("preheader"),
            connection=args.get("connection"))
    if name == "schedule_newsletter":
        return _schedule_newsletter(
            args["client"], args["send_on"], newsletter_id=args.get("newsletter_id"),
            name=args.get("name"), subject=args.get("subject"), html=args.get("html"),
            plain=args.get("plain", ""), campaign_id=args.get("campaign_id"),
            sender=args.get("sender"), preheader=args.get("preheader"),
            segment_ids=args.get("segment_ids"), connection=args.get("connection"))
    if name == "get_segments":
        return _get_segments(args["client"], connection=args.get("connection"))
    if name == "create_segment":
        return _create_segment(
            args["client"], args["name"], args["conditions"],
            campaign_ids=args.get("campaign_ids"), subscribers_type=args.get("subscribers_type"),
            condition_logic=args.get("condition_logic", "and"),
            section_logic=args.get("section_logic", "or"),
            subscriber_cycle=args.get("subscriber_cycle"),
            subscription_date=args.get("subscription_date", "all_time"),
            connection=args.get("connection"))
    if name == "upsert_contact":
        return _upsert_contact(
            args["client"], args["email"], campaign_id=args.get("campaign_id"),
            name=args.get("name"), tags=args.get("tags"),
            custom_fields=args.get("custom_fields"), day_of_cycle=args.get("day_of_cycle"),
            connection=args.get("connection"))
    if name == "trigger_automation_event":
        return _trigger_automation_event(
            args["client"], args["email"], campaign_id=args.get("campaign_id"),
            add_tags=args.get("add_tags"), set_custom_fields=args.get("set_custom_fields"),
            connection=args.get("connection"))
    if name == "get_campaign_statistics":
        return _get_campaign_statistics(
            args["client"], newsletter_ids=args.get("newsletter_ids"),
            campaign_ids=args.get("campaign_ids"),
            group_by=args.get("group_by", "total"), date_from=args.get("date_from"),
            date_to=args.get("date_to"), connection=args.get("connection"))
    return {"error": f"Unknown tool: {name}"}


# ── auto-seed legacy Iveresse on first boot ─────────────────────────────────

def _seed_iveresse():
    """One-time seed: if the store is empty but the old GR env vars exist,
    create an 'iveresse' client with a single 'getresponse' connection."""
    if not store.available():
        return
    try:
        if store.count_clients() > 0:
            return
        gr_key = os.environ.get("GR_API_KEY", "")
        if not gr_key:
            return
        store.add_connection(
            slug="iveresse", name="Iveresse", label="getresponse", esp_type="getresponse",
            credentials={"api_key": gr_key,
                         "base": os.environ.get("GR_BASE", "https://api.getresponse.com/v3")},
            defaults={"sender": os.environ.get("GR_DEFAULT_FROM_FIELD_ID", "rV7P7"),
                      "audience": os.environ.get("GR_DEFAULT_CAMPAIGN_ID", "L9fb4")},
        )
        logger.info("Seeded 'iveresse' client (getresponse connection) from legacy env vars")
    except Exception as e:
        logger.error(f"Seed iveresse failed: {e}")


# ── Starlette app ─────────────────────────────────────────────────────────────

async def health(request):
    return JSONResponse({"status": "ok", "server": "email-flow",
                         "store": store.available(), "clients": store.count_clients()})


async def dashboard(request):
    clients = store.list_clients() if store.available() else []
    rows = "".join(
        f'<tr><td>{c["slug"]}</td><td>{c["name"]}</td><td>'
        + ", ".join(f"{cn['label']} ({cn['esp_type']})" for cn in c["connections"])
        + "</td></tr>"
        for c in clients)
    store_ok = "✅" if store.available() else "❌ DATABASE_URL / MASTER_KEY missing"
    html = f"""<!DOCTYPE html><html><head><title>MCP Email Flow</title>
    <style>body{{font-family:system-ui;max-width:680px;margin:60px auto;padding:0 20px;color:#1a1a1a}}
    h1{{font-size:1.4rem}} table{{border-collapse:collapse;width:100%;margin:12px 0}}
    td,th{{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;font-size:14px}}
    .tag{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.8rem;background:#eef;margin:2px}}
    a{{color:#2563eb}}</style></head><body>
    <h1>MCP Email Flow — multi-client</h1>
    <p>Figma → ESP (GetResponse / Klaviyo) draft pipeline.</p>
    <p><b>Store:</b> {store_ok} · <b>Clients:</b> {len(clients)}</p>
    <table><tr><th>slug</th><th>name</th><th>ESP</th></tr>{rows or '<tr><td colspan=3>none</td></tr>'}</table>
    <p><a href="/admin">→ Admin panel</a> · <b>MCP:</b> <code>/mcp</code></p>
    <p><b>Tools:</b> {''.join(f'<span class="tag">{t.name}</span>' for t in ALL_TOOLS)}</p>
    </body></html>"""
    return HTMLResponse(html)


@contextlib.asynccontextmanager
async def lifespan(app):
    store.init_db()
    _seed_iveresse()
    async with session_manager.run():
        yield


_starlette_app = Starlette(
    routes=[Route("/", dashboard), Route("/health", health)] + admin.routes,
    lifespan=lifespan,
)
_starlette_app.add_middleware(CORSMiddleware, allow_origins=["*"],
                              allow_methods=["*"], allow_headers=["*"])


async def app(scope, receive, send):
    """Top-level ASGI dispatcher: /mcp goes straight to session_manager
    (avoids Starlette Mount trailing-slash redirect that drops POST bodies)."""
    if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/mcp":
        await session_manager.handle_request(scope, receive, send)
        return
    await _starlette_app(scope, receive, send)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting MCP Email Flow (multi-client) on port {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port)
