# MCP Email Flow

MCP server: Figma → GetResponse image pipeline for email newsletters.

## Tools
- `upload_from_figma_to_gr(file_key, node_id, name, scale=2)` — eksportuje jeden node z Figmy i uploaduje do GR CDN
- `bulk_upload_from_figma_to_gr(file_key, nodes, scale=2)` — hurtowy eksport + upload (jeden call do Figma API)
- `upload_url_to_gr(url, name)` — downloaduje dowolny publiczny URL i uploaduje do GR
- `list_gr_files(page, per_page)` — lista plików w GR gallery
- `check_config()` — sprawdza czy env vars są ustawione

## Env vars (Railway)
- `FIGMA_PAT` — Figma Personal Access Token
- `GR_API_KEY` — GetResponse API Key

## Endpoint
`/mcp` — SSE, stateless
