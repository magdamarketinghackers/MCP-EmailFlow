# MCP Email Flow

MCP server: Figma → GetResponse pipeline for email newsletters. Hosted on Railway.

## Architecture

Claude does Figma extraction via Figma MCP (OAuth — each user authenticates with their own Figma account).
This server has NO server-side Figma access — by design. Every team member's claude.ai needs:
1. Figma integration enabled in their account (claude.ai → Settings → Integrations → Figma)
2. Their Figma account must be in the team/project that owns the design file

This server only handles: image upload to GR File Library, draft creation in GR.

Same Starlette + SSE stack as the other 8 MCP servers — see `mcp_architecture.md` memory.

## Tools

**Main flow:**
- `bulk_upload_urls_to_gr(items)` — download images from URLs (e.g. Figma MCP S3) and upload to GR File Library. Auto-converts SVG → PNG via cairosvg.
- `create_gr_draft(name, subject, html, preheader, [from_field_id], [campaign_id])` — creates draft in GR Newsletters → Drafts. Sender/list defaults applied if not given.

**Helpers (rarely needed):**
- `upload_url_to_gr(url, name)` — single-image variant of bulk
- `list_gr_from_fields` — only if user wants a different sender
- `list_gr_campaigns` — only if user wants a different list
- `list_gr_drafts(name_filter, page, per_page)` — for management
- `delete_gr_drafts(newsletter_ids)` — bulk delete
- `check_config` — diagnostic

**GetResponse automation tools (GR connections only — error gracefully on Klaviyo):**
Added 2026-06 to extend MCP-EmailFlow from "Figma→draft" into near-full email/automation
marketing. All live against GR API v3, verified end-to-end.
- `create_newsletter_draft(client, name, subject, [html], [plain], [campaign_id], [sender], [preheader])`
  — editable draft; returns newsletterId.
- `schedule_newsletter(client, send_on, [newsletter_id]|[inline content], [campaign_id], [segment_ids], ...)`
  — schedules a **broadcast** for a future time. GR can't flip a draft to scheduled in-place,
  so passing `newsletter_id` COPIES the draft's content into a new scheduled broadcast
  (original draft stays). `send_on` = ISO 8601 w/ tz. Recipients: campaign_id and/or segment_ids.
- `get_segments(client)` — saved searches (search-contacts) → {id, name}.
- `create_segment(client, name, conditions, [campaign_ids], [condition_logic])`
  — `conditions`=[{conditionType, operator, value, operatorType}]. tag-exists:
  `{conditionType:"tag",operator:"exists",operatorType:"exists",value:"<tagId>"}`.
  **campaignIdsList is required by GR** — auto-filled with ALL lists if campaign_ids omitted.
- `upsert_contact(client, email, [campaign_id], [name], [tags], [custom_fields], [day_of_cycle])`
  — tags=NAMES (find-or-create), custom_fields={name:value} (find-or-create as text).
  Merges (doesn't wipe) existing tags/fields. Create returns 202 (async ~3s before findable).
- `trigger_automation_event(client, email, [campaign_id], [add_tags], [set_custom_fields])`
  — adds tags / sets fields on an EXISTING contact to fire GR Automation workflows.
- `get_campaign_statistics(client, [newsletter_ids]|[campaign_ids], [group_by], [date_from], [date_to])`
  — sent/delivered/opened/uniqueOpened/clicked/bounced/unsubscribed/complaints.
  **GR requires at least one of newsletter_ids or campaign_ids** (no unfiltered query).

## Env vars (Railway)

Required:
- `GR_API_KEY` — GetResponse API key

Optional:
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
