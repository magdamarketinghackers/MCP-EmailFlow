# MCP Email Flow

MCP server: Figma → GetResponse pipeline for email newsletters. Hosted on Railway.

## Architecture

Claude does Figma extraction via Figma MCP (OAuth — works without server-side PAT).
This server only handles: image upload to GR File Library, draft creation in GR.

Same Starlette + SSE stack as the other 8 MCP servers — see `mcp_architecture.md` memory.

## Tools

**Main flow:**
- `bulk_upload_urls_to_gr(items)` — download images from URLs (e.g. Figma MCP S3) and upload to GR File Library. Auto-converts SVG → PNG via cairosvg.
- `create_gr_draft(name, subject, html, preheader, [from_field_id], [campaign_id])` — creates draft in GR Newsletters → Drafts. Sender/list defaults applied if not given.

**Helpers (rarely needed):**
- `upload_url_to_gr(url, name)` — single-image variant of bulk
- `upload_from_figma_to_gr / bulk_upload_from_figma_to_gr` — legacy, use Figma REST API directly (requires FIGMA_PAT; Claude usually goes through Figma MCP instead)
- `list_gr_from_fields` — only if user wants a different sender
- `list_gr_campaigns` — only if user wants a different list
- `list_gr_drafts(name_filter, page, per_page)` — for management
- `delete_gr_drafts(newsletter_ids)` — bulk delete
- `check_config` — diagnostic

## Env vars (Railway)

Required:
- `GR_API_KEY` — GetResponse API key

Optional:
- `FIGMA_PAT` — only needed if using legacy `upload_from_figma_to_gr` tools
- `GR_BASE` — for GetResponse 360 (`https://api3.getresponse360.com/v3`); default is `https://api.getresponse.com/v3`
- `GR_DEFAULT_FROM_FIELD_ID` — default sender (defaults to `rV7P7` = IVERESSE)
- `GR_DEFAULT_CAMPAIGN_ID` — default list (defaults to `L9fb4` = Main)

## Critical GR API gotchas

- File Library endpoint: `POST /v3/file-library/files` (NOT `/v3/files`)
- File Library body: JSON with `{name, extension, content: <base64>, folder: null}`
- File Library does NOT accept SVG — server transparently converts via cairosvg
- cairosvg breaks on Figma's `var(--fill-0, #color)` and `width="100%"` — server preprocesses (regex strips var(), parses viewBox)
- Newsletter `sendSettings.selectedCampaigns` takes string IDs `["L9fb4"]`, NOT objects
- For drafts use `type: "draft"` (NOT broadcast + status)
- `{unsubscribe}` placeholder is added automatically by GR — never include in HTML

## Email HTML rules

- Width: 640px
- No `<!DOCTYPE>`, `<html>`, `<body>` — GR wraps everything
- Table-based layout for Outlook compatibility
- Font: Jost (already loaded in GR templates)
- **Font-weight: Figma 500 (Medium) → CSS 700 (Bold)** — email clients lack Medium variant
- `object-fit:cover; object-position:center` on photos (Outlook ignores it; Figma export crops correctly anyway)
- Button colors per campaign (Iveresse): `#C98695` Second Skin, `#7697B4` Last Call/Sport

## MCP endpoint

`/mcp` — Streamable HTTP, stateless. Health: `/health`.
