"""Read-only MCP server for Google Analytics 4, Google Search Console and Google Tag Manager.

Users sign in with Google (OAuth proxy). Only e-mails in ALLOWED_EMAILS can call tools.
The Google data is read with a service account (GOOGLE_SA_JSON).
"""

import json
import os
from functools import cache
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider, GoogleTokenVerifier
from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    GetMetadataRequest,
    RunRealtimeReportRequest,
    RunReportRequest,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/tagmanager.readonly",
]

ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
}
DEFAULT_PROPERTY = os.environ.get("GA4_PROPERTY_ID", "").strip()
DEFAULT_SITE = os.environ.get("GSC_SITE_URL", "").strip()


class AllowlistVerifier(GoogleTokenVerifier):
    """Rejects valid Google tokens whose e-mail is not in ALLOWED_EMAILS."""

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        email = (result.claims.get("email") or "").lower()
        if not result.claims.get("email_verified") or email not in ALLOWED_EMAILS:
            return None
        return result


def build_auth() -> GoogleProvider | None:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    if not client_id:
        if os.environ.get("ALLOW_NO_AUTH") == "1":
            return None
        raise SystemExit("GOOGLE_OAUTH_CLIENT_ID is not set. Set ALLOW_NO_AUTH=1 only for local runs.")
    provider = GoogleProvider(
        client_id=client_id,
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        base_url=os.environ["BASE_URL"],
        required_scopes=["openid", "email"],
        jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
        cache_ttl_seconds=300,
        extra_authorize_params={"prompt": "select_account consent"},
    )
    # GoogleProvider has no hook for an e-mail allowlist, so swap its verifier.
    provider._token_validator = AllowlistVerifier(
        required_scopes=["openid", "email"],
        cache_ttl_seconds=300,
        audience=client_id,
    )
    return provider


mcp = FastMCP(
    "gramodesky-analytics",
    instructions=(
        "Read-only access to Google Analytics 4 (ga4_* tools), Google Search Console (gsc_* tools) "
        "and Google Tag Manager (gtm_* tools). "
        "Call ga4_list_properties, gsc_list_sites or gtm_list_containers first if you do not know the IDs. "
        "Dates use YYYY-MM-DD or GA4 relative values (today, yesterday, 28daysAgo)."
    ),
    auth=build_auth(),
)


@cache
def credentials() -> service_account.Credentials:
    raw = os.environ.get("GOOGLE_SA_JSON", "")
    if not raw:
        raise ToolError("GOOGLE_SA_JSON is not set on the server.")
    return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)


@cache
def data_client() -> BetaAnalyticsDataClient:
    return BetaAnalyticsDataClient(credentials=credentials())


@cache
def admin_client() -> AnalyticsAdminServiceClient:
    return AnalyticsAdminServiceClient(credentials=credentials())


@cache
def gsc():
    return build("searchconsole", "v1", credentials=credentials(), cache_discovery=False)


@cache
def gtm():
    return build("tagmanager", "v2", credentials=credentials(), cache_discovery=False)


def property_name(property_id: str | None) -> str:
    pid = (property_id or DEFAULT_PROPERTY).removeprefix("properties/")
    if not pid:
        raise ToolError("Pass property_id (see ga4_list_properties).")
    return f"properties/{pid}"


def resolve_site(site: str | None) -> str:
    value = site or DEFAULT_SITE
    if not value:
        raise ToolError("Pass site_url (see gsc_list_sites), e.g. sc-domain:gramodesky.cz.")
    return value


def report_rows(response: Any) -> dict:
    dims = [h.name for h in response.dimension_headers]
    mets = [h.name for h in response.metric_headers]
    rows = [
        [v.value for v in row.dimension_values] + [v.value for v in row.metric_values]
        for row in response.rows
    ]
    return {"columns": dims + mets, "rows": rows, "row_count": response.row_count}


def gsc_call(request: Any) -> dict:
    try:
        return request.execute()
    except HttpError as e:
        raise ToolError(f"Search Console API error {e.status_code}: {e.reason}") from e


def gtm_call(request: Any) -> dict:
    try:
        return request.execute()
    except HttpError as e:
        raise ToolError(f"Tag Manager API error {e.status_code}: {e.reason}") from e


def gtm_containers() -> list[dict]:
    result = []
    for account in gtm_call(gtm().accounts().list()).get("account", []):
        containers = gtm_call(gtm().accounts().containers().list(parent=account["path"]))
        for c in containers.get("container", []):
            result.append({
                "account": account.get("name"),
                "public_id": c.get("publicId"),
                "name": c.get("name"),
                "domains": c.get("domainName", []),
                "path": c["path"],
            })
    return result


def gtm_container_path(container: str) -> str:
    if container.startswith("accounts/"):
        return container
    for c in gtm_containers():
        if c["public_id"] == container.upper():
            return c["path"]
    raise ToolError(f"Container {container} not found. See gtm_list_containers.")


def gtm_params(params: list[dict] | None) -> dict:
    """Turns the GTM parameter list into a plain dict."""
    def value(p: dict) -> Any:
        if p.get("type") == "list":
            return [value(x) for x in p.get("list", [])]
        if p.get("type") == "map":
            return {x.get("key"): value(x) for x in p.get("map", [])}
        return p.get("value")

    return {p.get("key"): value(p) for p in params or []}


def gtm_summary(data: dict, full: bool) -> dict:
    """Short view of tags, triggers and variables. Trigger IDs are replaced by trigger names."""
    triggers = {t["triggerId"]: t.get("name") for t in data.get("trigger", [])}
    builtin = {"2147479553": "All Pages", "2147479572": "Initialization - All Pages", "2147479573": "Consent Initialization - All Pages"}

    def names(ids: list[str] | None) -> list[str]:
        return [triggers.get(i) or builtin.get(i) or i for i in ids or []]

    tags = []
    for t in data.get("tag", []):
        tag = {
            "name": t.get("name"),
            "type": t.get("type"),
            "paused": t.get("paused", False),
            "firing": names(t.get("firingTriggerId")),
            "blocking": names(t.get("blockingTriggerId")),
            "consent": t.get("consentSettings"),
            "params": gtm_params(t.get("parameter")),
        }
        if full:
            tag["raw"] = t
        tags.append(tag)

    return {
        "tags": tags,
        "triggers": [
            {"name": t.get("name"), "type": t.get("type"), **({"raw": t} if full else {})}
            for t in data.get("trigger", [])
        ],
        "variables": [
            {"name": v.get("name"), "type": v.get("type"), "params": gtm_params(v.get("parameter"))}
            for v in data.get("variable", [])
        ],
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "features": ["ga4", "gsc", "gtm"]})


@mcp.tool(annotations={"readOnlyHint": True})
def ga4_list_properties() -> list[dict]:
    """List GA4 accounts and properties that the service account can read."""
    result = []
    for account in admin_client().list_account_summaries():
        for prop in account.property_summaries:
            result.append({
                "account": account.display_name,
                "property_id": prop.property.removeprefix("properties/"),
                "property_name": prop.display_name,
            })
    return result


@mcp.tool(annotations={"readOnlyHint": True})
def ga4_run_report(
    dimensions: Annotated[list[str], Field(description="GA4 API dimension names, e.g. date, sessionDefaultChannelGroup, pagePath")],
    metrics: Annotated[list[str], Field(description="GA4 API metric names, e.g. sessions, totalUsers, purchaseRevenue")],
    start_date: str = "28daysAgo",
    end_date: str = "yesterday",
    property_id: str | None = None,
    dimension_filter: Annotated[dict | None, Field(description="GA4 REST FilterExpression JSON, e.g. {\"filter\": {\"fieldName\": \"country\", \"stringFilter\": {\"value\": \"Czechia\"}}}")] = None,
    metric_filter: Annotated[dict | None, Field(description="GA4 REST FilterExpression JSON for metrics")] = None,
    order_bys: Annotated[list[dict] | None, Field(description="GA4 REST OrderBy list, e.g. [{\"metric\": {\"metricName\": \"sessions\"}, \"desc\": true}]")] = None,
    limit: Annotated[int, Field(ge=1, le=100000)] = 1000,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Run a GA4 report (Data API runReport). Use ga4_get_metadata to find dimension and metric names."""
    body: dict[str, Any] = {
        "property": property_name(property_id),
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "limit": limit,
        "offset": offset,
    }
    if dimension_filter:
        body["dimensionFilter"] = dimension_filter
    if metric_filter:
        body["metricFilter"] = metric_filter
    if order_bys:
        body["orderBys"] = order_bys
    request = RunReportRequest.from_json(json.dumps(body))
    return report_rows(data_client().run_report(request))


@mcp.tool(annotations={"readOnlyHint": True})
def ga4_run_realtime_report(
    metrics: Annotated[list[str], Field(description="Realtime metric names, e.g. activeUsers, eventCount")],
    dimensions: Annotated[list[str] | None, Field(description="Realtime dimension names, e.g. country, unifiedScreenName")] = None,
    property_id: str | None = None,
    limit: Annotated[int, Field(ge=1, le=100000)] = 100,
) -> dict:
    """Run a GA4 realtime report (last 30 minutes)."""
    body = {
        "property": property_name(property_id),
        "dimensions": [{"name": d} for d in dimensions or []],
        "metrics": [{"name": m} for m in metrics],
        "limit": limit,
    }
    request = RunRealtimeReportRequest.from_json(json.dumps(body))
    return report_rows(data_client().run_realtime_report(request))


@mcp.tool(annotations={"readOnlyHint": True})
def ga4_get_metadata(
    property_id: str | None = None,
    search: Annotated[str | None, Field(description="Case-insensitive text to filter names")] = None,
) -> dict:
    """List the dimensions and metrics (incl. custom ones) available for a GA4 property."""
    meta = data_client().get_metadata(GetMetadataRequest(name=f"{property_name(property_id)}/metadata"))
    needle = (search or "").lower()

    def keep(api_name: str, ui_name: str) -> bool:
        return not needle or needle in api_name.lower() or needle in ui_name.lower()

    return {
        "dimensions": [
            {"api_name": d.api_name, "ui_name": d.ui_name, "category": d.category}
            for d in meta.dimensions if keep(d.api_name, d.ui_name)
        ],
        "metrics": [
            {"api_name": m.api_name, "ui_name": m.ui_name, "category": m.category}
            for m in meta.metrics if keep(m.api_name, m.ui_name)
        ],
    }


@mcp.tool(annotations={"readOnlyHint": True})
def gsc_list_sites() -> list[dict]:
    """List Search Console properties that the service account can read."""
    return gsc_call(gsc().sites().list()).get("siteEntry", [])


@mcp.tool(annotations={"readOnlyHint": True})
def gsc_search_analytics(
    start_date: str,
    end_date: str,
    dimensions: Annotated[list[str] | None, Field(description="Any of: query, page, country, device, date, searchAppearance")] = None,
    site_url: str | None = None,
    search_type: Annotated[str, Field(description="web, image, video, news, discover, googleNews")] = "web",
    dimension_filter_groups: Annotated[list[dict] | None, Field(description="Search Console REST dimensionFilterGroups, e.g. [{\"filters\": [{\"dimension\": \"query\", \"operator\": \"contains\", \"expression\": \"vinyl\"}]}]")] = None,
    aggregation_type: Annotated[str, Field(description="auto, byPage, byProperty")] = "auto",
    data_state: Annotated[str, Field(description="final or all (all includes fresh data)")] = "final",
    row_limit: Annotated[int, Field(ge=1, le=25000)] = 1000,
    start_row: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Query Search Console performance data (clicks, impressions, CTR, position). Dates are YYYY-MM-DD."""
    body: dict[str, Any] = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions or [],
        "type": search_type,
        "aggregationType": aggregation_type,
        "dataState": data_state,
        "rowLimit": row_limit,
        "startRow": start_row,
    }
    if dimension_filter_groups:
        body["dimensionFilterGroups"] = dimension_filter_groups
    return gsc_call(gsc().searchanalytics().query(siteUrl=resolve_site(site_url), body=body))


@mcp.tool(annotations={"readOnlyHint": True})
def gsc_inspect_url(
    url: Annotated[str, Field(description="Full page URL to inspect")],
    site_url: str | None = None,
    language_code: str = "cs",
) -> dict:
    """Inspect the Google index status of one URL (URL Inspection API)."""
    body = {"inspectionUrl": url, "siteUrl": resolve_site(site_url), "languageCode": language_code}
    return gsc_call(gsc().urlInspection().index().inspect(body=body))


@mcp.tool(annotations={"readOnlyHint": True})
def gsc_list_sitemaps(site_url: str | None = None) -> list[dict]:
    """List the sitemaps of a Search Console property with their status."""
    return gsc_call(gsc().sitemaps().list(siteUrl=resolve_site(site_url))).get("sitemap", [])


@mcp.tool(annotations={"readOnlyHint": True})
def gtm_list_containers() -> list[dict]:
    """List GTM accounts and containers (public ID GTM-XXXX, domains, API path) that the service account can read."""
    return gtm_containers()


@mcp.tool(annotations={"readOnlyHint": True})
def gtm_get_live_version(
    container: Annotated[str, Field(description="Public ID (GTM-XXXX) or API path accounts/<id>/containers/<id>")],
    full: Annotated[bool, Field(description="Include the raw API objects of tags and triggers")] = False,
) -> dict:
    """Show the published version of a container: tags with triggers, consent settings and parameters (e.g. GA4 tagId)."""
    version = gtm_call(gtm().accounts().containers().versions().live(parent=gtm_container_path(container)))
    return {
        "version_id": version.get("containerVersionId"),
        "version_name": version.get("name"),
        "description": version.get("description"),
        **gtm_summary(version, full),
    }


@mcp.tool(annotations={"readOnlyHint": True})
def gtm_list_versions(
    container: Annotated[str, Field(description="Public ID (GTM-XXXX) or API path")],
) -> list[dict]:
    """List the version history of a container (ID, name, counts of tags and triggers)."""
    path = gtm_container_path(container)
    headers = gtm_call(gtm().accounts().containers().version_headers().list(parent=path))
    return [
        {
            "version_id": h.get("containerVersionId"),
            "name": h.get("name"),
            "deleted": h.get("deleted", False),
            "tags": h.get("numTags"),
            "triggers": h.get("numTriggers"),
            "variables": h.get("numVariables"),
        }
        for h in headers.get("containerVersionHeader", [])
    ]


@mcp.tool(annotations={"readOnlyHint": True})
def gtm_get_version(
    container: Annotated[str, Field(description="Public ID (GTM-XXXX) or API path")],
    version_id: str,
    full: bool = False,
) -> dict:
    """Show one older version of a container in the same form as gtm_get_live_version."""
    path = f"{gtm_container_path(container)}/versions/{version_id}"
    version = gtm_call(gtm().accounts().containers().versions().get(path=path))
    return {"version_id": version.get("containerVersionId"), "version_name": version.get("name"), **gtm_summary(version, full)}


@mcp.tool(annotations={"readOnlyHint": True})
def gtm_get_workspace_changes(
    container: Annotated[str, Field(description="Public ID (GTM-XXXX) or API path")],
) -> list[dict]:
    """List the workspaces of a container and their changes that are not published yet."""
    path = gtm_container_path(container)
    result = []
    for ws in gtm_call(gtm().accounts().containers().workspaces().list(parent=path)).get("workspace", []):
        status = gtm_call(gtm().accounts().containers().workspaces().getStatus(path=ws["path"]))
        changes = []
        for change in status.get("workspaceChange", []):
            kind = next((k for k in ("tag", "trigger", "variable", "folder", "client", "transformation") if k in change), None)
            entity = change.get(kind, {}) if kind else {}
            changes.append({"status": change.get("changeStatus"), "kind": kind, "name": entity.get("name")})
        result.append({"workspace": ws.get("name"), "changes": changes, "conflicts": len(status.get("mergeConflict", []))})
    return result


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), path="/mcp")
