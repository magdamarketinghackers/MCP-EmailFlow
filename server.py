import logging
import os
import json
import traceback
import urllib.parse
import uvicorn
import httpx
import contextlib
import hashlib
import hmac
import time
import base64
import cairosvg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

from typing import Dict, Any, List, Optional

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

# ── Config ────────────────────────────────────────────────────────────────────

FIGMA_PAT        = os.environ.get("FIGMA_PAT", "")
GR_API_KEY       = os.environ.get("GR_API_KEY", "")
GR_BASE          = os.environ.get("GR_BASE", "https://api.getresponse.com/v3").rstrip("/")
CLOUDINARY_URL   = os.environ.get("CLOUDINARY_URL", "")  # cloudinary://api_key:api_secret@cloud_name

server          = Server("email-flow")
session_manager = StreamableHTTPSessionManager(app=server, stateless=True)


# ── Figma helpers ─────────────────────────────────────────────────────────────

def figma_export_urls(file_key: str, node_ids: List[str], scale: int = 2) -> Dict[str, str]:
    """
    Calls Figma Images API to get S3 export URLs for the given node IDs.
    Each node is rendered at its exact design dimensions (with crop / object-fit:cover applied).
    Returns {nodeId: s3Url}.
    """
    ids_param = urllib.parse.quote(",".join(node_ids))
    url = f"https://api.figma.com/v1/images/{file_key}?ids={ids_param}&format=png&scale={scale}"
    with httpx.Client(timeout=30) as c:
        r = c.get(url, headers={"X-Figma-Token": FIGMA_PAT})
        r.raise_for_status()
        data = r.json()
    if data.get("err"):
        raise ValueError(f"Figma API error: {data['err']}")
    return data.get("images", {})


def figma_download(s3_url: str) -> bytes:
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        r = c.get(s3_url)
        r.raise_for_status()
    return r.content


def _normalize_node_id(node_id: str) -> str:
    """Figma may return nodeIds as '923:87' or '923-87'; normalise to ':'."""
    return node_id.replace("-", ":") if "-" in node_id and ":" not in node_id else node_id


def _mime(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")


# ── Cloudinary helpers ────────────────────────────────────────────────────────

def _parse_cloudinary_url(url: str):
    """Parse cloudinary://api_key:api_secret@cloud_name"""
    if not url.startswith("cloudinary://"):
        raise ValueError("CLOUDINARY_URL must be cloudinary://api_key:api_secret@cloud_name")
    rest = url[len("cloudinary://"):]
    creds, cloud_name = rest.rsplit("@", 1)
    api_key, api_secret = creds.split(":", 1)
    return api_key, api_secret, cloud_name


def cloudinary_upload(image_bytes: bytes, public_id: str) -> str:
    """Upload image to Cloudinary, return secure CDN URL."""
    api_key, api_secret, cloud_name = _parse_cloudinary_url(CLOUDINARY_URL)
    ts = str(int(time.time()))
    params_to_sign = f"public_id={public_id}&timestamp={ts}"
    signature = hmac.new(api_secret.encode(), params_to_sign.encode(), hashlib.sha1).hexdigest()
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
    with httpx.Client(timeout=120) as c:
        r = c.post(url, data={
            "api_key":   api_key,
            "timestamp": ts,
            "public_id": public_id,
            "signature": signature,
        }, files={"file": (f"{public_id}.png", image_bytes, "image/png")})
        r.raise_for_status()
    return r.json()["secure_url"]


# ── GetResponse Files helpers ──────────────────────────────────────────────────

def gr_headers() -> Dict[str, str]:
    return {"X-Auth-Token": f"api-key {GR_API_KEY}"}


def gr_upload(image_bytes: bytes, filename: str) -> str:
    """
    Uploads an image to GetResponse File Library.
    Returns the public CDN URL.
    """
    name, _, ext = filename.rpartition(".")
    if not name:
        name, ext = filename, "png"

    payload = {
        "name":      name,
        "extension": ext,
        "content":   base64.b64encode(image_bytes).decode("ascii"),
        "folder":    None,
    }
    with httpx.Client(timeout=120) as c:
        r = c.post(
            f"{GR_BASE}/file-library/files",
            headers={**gr_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()

    cdn_url = (data.get("url") or data.get("publicUrl") or
               data.get("fileUrl") or data.get("src") or
               (data.get("file") or {}).get("url"))
    if not cdn_url:
        raise ValueError(f"GR upload succeeded but no URL in response: {json.dumps(data)}")
    return cdn_url


def _is_svg(content: bytes, content_type: str = "") -> bool:
    if "svg" in content_type.lower():
        return True
    head = content[:512].lstrip().lower()
    return head.startswith(b"<?xml") and b"<svg" in head[:512] or head.startswith(b"<svg")


def _svg_to_png(svg_bytes: bytes, scale: int = 4) -> bytes:
    """Render SVG to PNG. scale upsamples small icons for crisp display."""
    return cairosvg.svg2png(bytestring=svg_bytes, scale=scale)


def upload_image(image_bytes: bytes, filename: str, content_type: str = "") -> tuple[str, str]:
    """
    Upload image to best available CDN.
    Returns (cdn_url, provider) where provider is 'getresponse' or 'cloudinary'.
    Tries GR first; falls back to Cloudinary if GR returns 404.
    Converts SVG to PNG before upload (GR File Library doesn't accept SVG).
    """
    if _is_svg(image_bytes, content_type):
        image_bytes = _svg_to_png(image_bytes)
        base = filename.rsplit(".", 1)[0]
        filename = f"{base}.png"
    try:
        url = gr_upload(image_bytes, filename)
        return url, "getresponse"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404 and CLOUDINARY_URL:
            public_id = filename.rsplit(".", 1)[0]
            url = cloudinary_upload(image_bytes, public_id)
            return url, "cloudinary"
        raise


def gr_list(page: int = 1, per_page: int = 100) -> List[Dict]:
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{GR_BASE}/file-library/files",
                  headers=gr_headers(),
                  params={"page": page, "perPage": per_page})
        r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("files", data.get("items", []))


# ── Tool implementations ───────────────────────────────────────────────────────

def _upload_from_figma_to_gr(file_key: str, node_id: str, name: str, scale: int = 2) -> Dict:
    """Export one Figma node and upload to GR. Returns gr_url."""
    if not FIGMA_PAT:
        return {"error": "FIGMA_PAT env var not configured on Railway"}
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}

    nid = _normalize_node_id(node_id)
    try:
        export_map = figma_export_urls(file_key, [nid], scale)
    except Exception as e:
        return {"error": f"Figma export failed: {e}"}

    s3_url = export_map.get(nid) or export_map.get(node_id)
    if not s3_url:
        return {"error": f"No Figma export URL for nodeId '{nid}'. Got: {list(export_map.keys())}"}

    try:
        image_bytes = figma_download(s3_url)
    except Exception as e:
        return {"error": f"Image download failed: {e}"}

    filename = f"{name}.png"
    try:
        cdn_url, provider = upload_image(image_bytes, filename)
    except Exception as e:
        return {"error": f"Upload failed: {e}"}

    logger.info(f"Uploaded {filename} via {provider} → {cdn_url}")
    return {"cdn_url": cdn_url, "provider": provider, "name": name, "filename": filename, "size_bytes": len(image_bytes)}


def _bulk_upload_from_figma_to_gr(file_key: str, nodes: List[Dict], scale: int = 2) -> Dict:
    """
    Bulk export + upload. nodes: [{nodeId, name}, ...].
    One Figma API call for all nodes, then uploads sequentially.
    Returns {uploaded: {name: gr_url}, errors: {name: reason}}.
    """
    if not FIGMA_PAT:
        return {"error": "FIGMA_PAT env var not configured on Railway"}
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}

    node_ids = [_normalize_node_id(n["nodeId"]) for n in nodes]

    try:
        export_map = figma_export_urls(file_key, node_ids, scale)
    except Exception as e:
        return {"error": f"Figma batch export failed: {e}"}

    uploaded: Dict[str, str] = {}
    errors:   Dict[str, str] = {}

    for node in nodes:
        nid  = _normalize_node_id(node["nodeId"])
        name = node["name"]
        s3   = export_map.get(nid) or export_map.get(node["nodeId"])
        if not s3:
            errors[name] = f"No Figma export URL for nodeId '{nid}'"
            continue
        try:
            img = figma_download(s3)
            cdn_url, provider = upload_image(img, f"{name}.png")
            uploaded[name] = cdn_url
            logger.info(f"  ✓ {name} via {provider} → {cdn_url}")
        except Exception as e:
            errors[name] = str(e)
            logger.error(f"  ✗ {name}: {e}")

    return {
        "uploaded": uploaded,
        "errors":   errors,
        "total":    len(nodes),
        "success":  len(uploaded),
        "failed":   len(errors),
    }


def _bulk_upload_urls_to_gr(items: List[Dict]) -> Dict:
    """
    Bulk upload images from URLs to GR File Library.
    items: [{url, name}, ...]
    Returns {uploaded: {name: gr_url}, errors: {name: reason}}.
    """
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}

    uploaded: Dict[str, str] = {}
    errors:   Dict[str, str] = {}

    for item in items:
        url  = item["url"]
        name = item["name"]
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as c:
                r = c.get(url)
                r.raise_for_status()
                img = r.content
                ct  = r.headers.get("content-type", "")
            if "svg" in ct.lower() or _is_svg(img, ct):
                ext = "svg"
            elif "jpeg" in ct:
                ext = "jpg"
            else:
                ext = "png"
            cdn_url, provider = upload_image(img, f"{name}.{ext}", content_type=ct)
            uploaded[name] = cdn_url
            logger.info(f"  ✓ {name} via {provider} → {cdn_url}")
        except Exception as e:
            errors[name] = str(e)
            logger.error(f"  ✗ {name}: {e}")

    return {
        "uploaded": uploaded,
        "errors":   errors,
        "total":    len(items),
        "success":  len(uploaded),
        "failed":   len(errors),
    }


def _upload_url_to_gr(url: str, name: str) -> Dict:
    """Download any public URL and upload to GR. Useful for logos, icons."""
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            img = r.content
            ct  = r.headers.get("content-type", "")
    except Exception as e:
        return {"error": f"Download failed: {e}"}

    if "svg" in ct.lower() or _is_svg(img, ct):
        ext = "svg"
    elif "jpeg" in ct:
        ext = "jpg"
    else:
        ext = "png"
    filename = f"{name}.{ext}"
    try:
        cdn_url, provider = upload_image(img, filename, content_type=ct)
    except Exception as e:
        return {"error": f"Upload failed: {e}"}

    return {"cdn_url": cdn_url, "provider": provider, "name": name, "filename": filename}


def _list_gr_files(page: int = 1, per_page: int = 100) -> Dict:
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}
    try:
        files = gr_list(page, per_page)
        return {"files": files, "count": len(files), "page": page}
    except Exception as e:
        return {"error": str(e)}


def _test_figma_token() -> Dict:
    """
    Test the FIGMA_PAT against key endpoints.
    Note: /v1/me is excluded for Plan (org) access tokens — we skip it.
    """
    if not FIGMA_PAT:
        return {"error": "FIGMA_PAT not set"}
    file_key = "LchycCBdOmUOuABklxHXqp"
    headers  = {"X-Figma-Token": FIGMA_PAT, "User-Agent": "Mozilla/5.0"}
    results  = {"token_prefix": FIGMA_PAT[:10] + "..."}
    try:
        with httpx.Client(timeout=15) as c:
            # Try X-Figma-Token header (standard PAT)
            r1 = c.get(f"https://api.figma.com/v1/files/{file_key}?depth=1", headers={"X-Figma-Token": FIGMA_PAT})
            results["x_figma_token_status"] = r1.status_code

            # Try Authorization: Bearer (OAuth / developer tokens)
            r2 = c.get(f"https://api.figma.com/v1/files/{file_key}?depth=1",
                       headers={"Authorization": f"Bearer {FIGMA_PAT}"})
            results["bearer_status"] = r2.status_code

            if r1.status_code == 200:
                results["auth_method"] = "X-Figma-Token"
                results["file_name"] = r1.json().get("name")
            elif r2.status_code == 200:
                results["auth_method"] = "Bearer"
                results["file_name"] = r2.json().get("name")

            # Image export with whichever header worked
            working_headers = ({"X-Figma-Token": FIGMA_PAT} if r1.status_code == 200
                               else {"Authorization": f"Bearer {FIGMA_PAT}"})
            r3 = c.get(
                f"https://api.figma.com/v1/images/{file_key}?ids=923%3A83&format=png&scale=1",
                headers=working_headers
            )
            results["image_export_status"] = r3.status_code
            if r3.status_code == 200:
                results["image_export_ok"] = True
            else:
                results["image_export_error"] = r3.text[:200]
    except Exception as e:
        results["error"] = str(e)
    return results


def _check_config() -> Dict:
    cdn = "cloudinary" if CLOUDINARY_URL else "getresponse_files"
    return {
        "figma_pat_set":      bool(FIGMA_PAT),
        "gr_api_key_set":     bool(GR_API_KEY),
        "cloudinary_set":     bool(CLOUDINARY_URL),
        "image_cdn":          cdn,
        "gr_base":            GR_BASE,
        "status": "ok" if (FIGMA_PAT and GR_API_KEY) else "missing_credentials",
    }


def _list_gr_from_fields() -> Dict:
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{GR_BASE}/from-fields", headers=gr_headers())
            r.raise_for_status()
        fields = r.json()
        return {"from_fields": fields, "count": len(fields)}
    except Exception as e:
        return {"error": str(e)}


def _list_gr_campaigns(page: int = 1, per_page: int = 100) -> Dict:
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}
    try:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{GR_BASE}/campaigns", headers=gr_headers(),
                      params={"page": page, "perPage": per_page})
            r.raise_for_status()
        campaigns = r.json()
        if isinstance(campaigns, dict):
            campaigns = campaigns.get("campaigns", [])
        simplified = [{"campaignId": c.get("campaignId"), "name": c.get("name"),
                       "languageCode": c.get("languageCode")} for c in campaigns]
        return {"campaigns": simplified, "count": len(simplified)}
    except Exception as e:
        return {"error": str(e)}


def _create_gr_draft(name: str, subject: str, html: str,
                     from_field_id: str, campaign_id: str,
                     preheader: Optional[str] = None) -> Dict:
    if not GR_API_KEY:
        return {"error": "GR_API_KEY env var not configured on Railway"}

    # Inject preheader as hidden span before body content if provided
    if preheader:
        preheader_span = (
            f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
            f'{preheader}'
            f'</div>'
        )
        # Insert after first <table or at the very beginning if no table found
        if "<table" in html:
            html = html.replace("<table", preheader_span + "<table", 1)
        else:
            html = preheader_span + html

    payload = {
        "name": name,
        "type": "broadcast",
        "status": "draft",
        "subject": subject,
        "fromField": {"fromFieldId": from_field_id},
        "replyTo":   {"fromFieldId": from_field_id},
        "campaign":  {"campaignId": campaign_id},
        "content": {
            "html":  html,
            "plain": "",
        },
        "flags": ["openrate", "clicktrack"],
        "sendSettings": {
            "selectedCampaigns": [{"campaignId": campaign_id}],
        },
    }

    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{GR_BASE}/newsletters", headers={
                **gr_headers(), "Content-Type": "application/json"
            }, json=payload)
            r.raise_for_status()
        data = r.json()
        newsletter_id = data.get("newsletterId") or data.get("id")
        logger.info(f"Draft created: {newsletter_id} — '{name}'")
        return {
            "newsletterId": newsletter_id,
            "name": name,
            "subject": subject,
            "status": data.get("status", "draft"),
            "href": data.get("href"),
        }
    except httpx.HTTPStatusError as e:
        return {"error": f"GR API error {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


# ── MCP tool registry ─────────────────────────────────────────────────────────

ALL_TOOLS = [
    Tool(
        name="upload_from_figma_to_gr",
        description=(
            "Export a single Figma node as PNG (rendered with exact crop, equivalent to "
            "object-fit:cover) and upload it to GetResponse Files CDN. "
            "Returns the permanent GR CDN URL ready to use in email HTML. "
            "Use the node-id from Figma URL or from data-node-id attributes in design context."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key (from URL: figma.com/design/{fileKey}/...)"},
                "node_id":  {"type": "string", "description": "Figma node ID, e.g. '923:87'"},
                "name":     {"type": "string", "description": "Output filename (without extension), e.g. 'hero'"},
                "scale":    {"type": "integer", "description": "Export scale: 1=1x, 2=2x retina (default)", "default": 2},
            },
            "required": ["file_key", "node_id", "name"],
        },
    ),
    Tool(
        name="bulk_upload_from_figma_to_gr",
        description=(
            "Export multiple Figma nodes in a single API call and upload all to GetResponse Files. "
            "More efficient than calling upload_from_figma_to_gr repeatedly. "
            "Returns mapping of name → GR CDN URL for all successfully uploaded images."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_key": {"type": "string", "description": "Figma file key"},
                "nodes": {
                    "type": "array",
                    "description": "List of nodes to export",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nodeId": {"type": "string", "description": "Figma node ID, e.g. '923:87'"},
                            "name":   {"type": "string", "description": "Output filename (no extension)"},
                        },
                        "required": ["nodeId", "name"],
                    },
                },
                "scale": {"type": "integer", "description": "Export scale: 1 or 2 (retina, default)", "default": 2},
            },
            "required": ["file_key", "nodes"],
        },
    ),
    Tool(
        name="bulk_upload_urls_to_gr",
        description=(
            "Bulk download images from URLs and upload to GetResponse File Library. "
            "Most efficient way to push Figma images to GR — Claude fetches Figma design context "
            "(via Figma MCP/OAuth) and passes the resulting image URLs here."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "List of {url, name} pairs",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url":  {"type": "string", "description": "Public image URL (e.g. Figma S3 export URL)"},
                            "name": {"type": "string", "description": "Output filename (no extension)"},
                        },
                        "required": ["url", "name"],
                    },
                },
            },
            "required": ["items"],
        },
    ),
    Tool(
        name="upload_url_to_gr",
        description=(
            "Download any public image URL and upload it to GetResponse Files CDN. "
            "Useful for uploading icons, logos, or images from other sources."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url":  {"type": "string", "description": "Public image URL to download"},
                "name": {"type": "string", "description": "Output filename (without extension)"},
            },
            "required": ["url", "name"],
        },
    ),
    Tool(
        name="list_gr_files",
        description="List files already uploaded to GetResponse Files gallery.",
        inputSchema={
            "type": "object",
            "properties": {
                "page":     {"type": "integer", "description": "Page number (default 1)", "default": 1},
                "per_page": {"type": "integer", "description": "Results per page (default 100)", "default": 100},
            },
        },
    ),
    Tool(
        name="test_figma_token",
        description="Debug: test if FIGMA_PAT is valid and has access to the Iveresse file.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="check_config",
        description="Check whether FIGMA_PAT and GR_API_KEY are configured on this server.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_gr_from_fields",
        description=(
            "List available sender (From) addresses configured in GetResponse. "
            "Returns fromFieldId and email for each. Required before calling create_gr_draft."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_gr_campaigns",
        description=(
            "List subscriber lists (campaigns) in GetResponse. "
            "Returns campaignId and name. Required before calling create_gr_draft."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "page":     {"type": "integer", "description": "Page number (default 1)", "default": 1},
                "per_page": {"type": "integer", "description": "Results per page (default 100)", "default": 100},
            },
        },
    ),
    Tool(
        name="create_gr_draft",
        description=(
            "Create a newsletter draft in GetResponse with the provided HTML. "
            "The draft appears in GetResponse → Newsletters → Drafts and is ready to schedule or send. "
            "Call list_gr_from_fields and list_gr_campaigns first to obtain the required IDs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name":           {"type": "string", "description": "Internal newsletter name (visible only in GR dashboard)"},
                "subject":        {"type": "string", "description": "Email subject line shown to recipients"},
                "html":           {"type": "string", "description": "Full HTML content of the email"},
                "from_field_id":  {"type": "string", "description": "fromFieldId from list_gr_from_fields"},
                "campaign_id":    {"type": "string", "description": "campaignId from list_gr_campaigns"},
                "preheader":      {"type": "string", "description": "Optional preheader / preview text (injected as hidden span)"},
            },
            "required": ["name", "subject", "html", "from_field_id", "campaign_id"],
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
    if name == "upload_from_figma_to_gr":
        return _upload_from_figma_to_gr(
            args["file_key"], args["node_id"], args["name"],
            scale=args.get("scale", 2),
        )
    if name == "bulk_upload_from_figma_to_gr":
        return _bulk_upload_from_figma_to_gr(
            args["file_key"], args["nodes"],
            scale=args.get("scale", 2),
        )
    if name == "upload_url_to_gr":
        return _upload_url_to_gr(args["url"], args["name"])
    if name == "bulk_upload_urls_to_gr":
        return _bulk_upload_urls_to_gr(args["items"])
    if name == "list_gr_files":
        return _list_gr_files(args.get("page", 1), args.get("per_page", 100))
    if name == "check_config":
        return _check_config()
    if name == "test_figma_token":
        return _test_figma_token()
    if name == "list_gr_from_fields":
        return _list_gr_from_fields()
    if name == "list_gr_campaigns":
        return _list_gr_campaigns(args.get("page", 1), args.get("per_page", 100))
    if name == "create_gr_draft":
        return _create_gr_draft(
            args["name"], args["subject"], args["html"],
            args["from_field_id"], args["campaign_id"],
            preheader=args.get("preheader"),
        )
    return {"error": f"Unknown tool: {name}"}


# ── Starlette app ─────────────────────────────────────────────────────────────

async def health(request):
    return JSONResponse({"status": "ok", "server": "email-flow"})


async def dashboard(request):
    figma_ok   = "✅" if FIGMA_PAT      else "❌ FIGMA_PAT not set"
    gr_ok      = "✅" if GR_API_KEY     else "❌ GR_API_KEY not set"
    cloud_ok   = "✅" if CLOUDINARY_URL else "⚠️ not set (will use GR Files)"
    cdn_active = "Cloudinary" if CLOUDINARY_URL else "GetResponse Files"
    html = f"""<!DOCTYPE html><html><head><title>MCP Email Flow</title>
    <style>body{{font-family:system-ui;max-width:600px;margin:60px auto;padding:0 20px;color:#1a1a1a}}
    h1{{font-size:1.4rem;font-weight:600}}
    .tag{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.85rem;background:#f0f0f0;margin:4px 0}}
    </style></head><body>
    <h1>MCP Email Flow</h1>
    <p>Figma → GetResponse image pipeline for email newsletters.</p>
    <p><b>Figma PAT:</b> {figma_ok}</p>
    <p><b>GR API Key:</b> {gr_ok}</p>
    <p><b>Cloudinary:</b> {cloud_ok}</p>
    <p><b>Image CDN:</b> {cdn_active}</p>
    <p><b>GR API base:</b> <code>{GR_BASE}</code></p>
    <p><b>MCP endpoint:</b> <code>/mcp</code></p>
    <hr>
    <p><b>Tools:</b></p>
    {''.join(f'<span class="tag">{t.name}</span><br>' for t in ALL_TOOLS)}
    </body></html>"""
    return HTMLResponse(html)


async def mcp_asgi(scope, receive, send):
    await session_manager.handle_request(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(app):
    async with session_manager.run():
        yield


_starlette_app = Starlette(
    routes=[
        Route("/",       endpoint=dashboard),
        Route("/health", endpoint=health),
    ],
    lifespan=lifespan,
)
_starlette_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def app(scope, receive, send):
    """Top-level ASGI dispatcher: /mcp goes directly to session_manager
    (avoids Starlette Mount trailing-slash redirect that drops POST bodies)."""
    if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/mcp":
        await session_manager.handle_request(scope, receive, send)
        return
    await _starlette_app(scope, receive, send)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting MCP Email Flow on port {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port)
