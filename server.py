"""Read-only MCP server for Google Analytics 4 and Google Search Console.

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
        "Read-only access to Google Analytics 4 (ga4_* tools) and Google Search Console (gsc_* tools). "
        "Call ga4_list_properties or gsc_list_sites first if you do not know the IDs. "
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


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


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), path="/mcp")
