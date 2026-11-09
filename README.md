# analytics-mcp-http

Read-only remote MCP server for Google Analytics 4, Google Search Console and Google Tag Manager (Streamable HTTP, FastMCP).

Users sign in with Google. Only e-mails in `ALLOWED_EMAILS` can call tools.
The server reads Google data with a service account.

## Tools

- `ga4_list_properties`, `ga4_run_report`, `ga4_run_realtime_report`, `ga4_get_metadata`
- `gsc_list_sites`, `gsc_search_analytics`, `gsc_inspect_url`, `gsc_list_sitemaps`
- `gtm_list_containers`, `gtm_get_live_version`, `gtm_list_versions`, `gtm_get_version`, `gtm_get_workspace_changes`, `gtm_get_workspace` (read)
- `gtm_create_workspace`, `gtm_set_tag_parameter`, `gtm_create_tag` (write, only in workspaces named `MCP: …`; there is no publish tool)

## Env

- `GOOGLE_SA_JSON` — full JSON key of the service account (secret)
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` — Google OAuth client (Web application)
- `BASE_URL` — public URL, e.g. `https://analytics-mcp.gramodesky.cz`
- `JWT_SIGNING_KEY` — random secret for the tokens that the server gives to MCP clients
- `ALLOWED_EMAILS` — comma-separated list of Google e-mails
- `GA4_PROPERTY_ID`, `GSC_SITE_URL` — optional defaults (e.g. `sc-domain:gramodesky.cz`)
- `FASTMCP_HOME` — `/data`. Mount a persistent volume there.

If `GOOGLE_OAUTH_CLIENT_ID` is not set, the server does not start. For a local run without auth, set `ALLOW_NO_AUTH=1`.

## Google setup

1. Enable Analytics Data API, Analytics Admin API, Search Console API and Tag Manager API.
2. Add the service account e-mail to GA4 (Viewer) and to Search Console (Restricted).
   In each GTM account, add it in Admin → User Management with account access "User" and container access "Read" (or "Edit" for the write tools; never "Publish").
3. OAuth client redirect URI: `https://analytics-mcp.gramodesky.cz/auth/callback`

## Endpoints

- `GET /health` — healthcheck
- `/mcp` — MCP Streamable HTTP (OAuth)

## Clients

- Claude: Settings → Connectors → Add custom connector → `https://analytics-mcp.gramodesky.cz/mcp`
- Cursor: `.cursor/mcp.json` → `{"mcpServers": {"analytics": {"url": "https://analytics-mcp.gramodesky.cz/mcp"}}}`
