import time
import requests
import boto3
import calendar
from datetime import datetime, timedelta, timezone

def utcnow():
    """Timezone-aware UTC now — replaces deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc)

from config import (
    # Azure AD / Graph API
    TENANT_ID, CLIENT_ID, CLIENT_SECRET, FROM_EMAIL, TO_EMAIL, CC_EMAIL,

    # AWS
    AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION,

    # Azure
    AZURE_SUBSCRIPTION_ID, AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,

    # GCP
    GCP_DATASET_TABLE,
    GCP_SERVICE_ACCOUNT_JSON,
    GCP_BILLING_ACCOUNT_ID,
    GCP_PROJECT_ID,

    # Shared config
    REPORT_DAY,
    EMAIL_NOTE,
)


# ─────────────────────────────────────────────
# Currency: INR → USD
# ─────────────────────────────────────────────
def get_inr_to_usd_rate() -> float:
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        inr_per_usd = data["rates"]["INR"]
        rate = round(1.0 / inr_per_usd, 6)
        print(f"  💱 Live INR→USD rate: 1 INR = {rate:.6f} USD  (1 USD = {inr_per_usd:.2f} INR)")
        return rate
    except Exception as e:
        fallback = 0.012
        print(f"  ⚠️  Could not fetch live rate ({e}); using fallback {fallback}")
        return fallback


def convert_inr_dict_to_usd(data: dict, rate: float) -> dict:
    if "ERROR" in data:
        return data
    return {svc: round(amt * rate, 2) for svc, amt in data.items()}


# ─────────────────────────────────────────────
# Date Ranges
# ─────────────────────────────────────────────
def get_date_range():
    """
    Returns (start, end, is_month_end).

    is_month_end is True when REPORT_DAY (clamped to the number of days
    in the current month) equals the last day of the month — meaning
    this run represents a closing/final report for the month, not a
    mid-month snapshot, so forecasts should be omitted.

    REPORT_DAY is clamped instead of passed straight into .replace(day=...)
    because e.g. REPORT_DAY=31 would crash with a ValueError in any
    30-day or 28/29-day month.
    """
    today = utcnow().date()
    start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    report_day_clamped = min(int(REPORT_DAY), days_in_month)
    end = min(today, today.replace(day=report_day_clamped)) + timedelta(days=1)
    is_month_end = report_day_clamped >= days_in_month
    return str(start), str(end), is_month_end


def get_gcp_date_range():
    return get_date_range()


# ─────────────────────────────────────────────
# Forecast helpers
# ─────────────────────────────────────────────
def compute_forecast(actual_total: float, start_str: str, end_str: str,
                     forecast_override: float = None, skip: bool = False) -> dict | None:
    """
    skip=True returns None — used for month-end reports where a forecast
    is meaningless (the month is already over).
    """
    if skip:
        return None

    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end   = datetime.strptime(end_str,   "%Y-%m-%d").date()

    days_elapsed  = (end - start).days
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    pct_done      = days_elapsed / days_in_month

    if forecast_override is not None:
        forecast = round(forecast_override, 2)
    else:
        forecast = round((actual_total / pct_done), 2) if pct_done > 0 else 0.0

    return {
        "days_elapsed":   days_elapsed,
        "days_in_month":  days_in_month,
        "forecast":       forecast,
        "pct_month_done": round(pct_done * 100, 1),
    }


# ─────────────────────────────────────────────
# AWS — Cost (MTD actual)
# ─────────────────────────────────────────────
def fetch_aws_cost(start, end):
    try:
        client = boto3.client(
            "ce",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
        )
        response = client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        results = {}
        for result_by_time in response["ResultsByTime"]:
            for group in result_by_time["Groups"]:
                service = group["Keys"][0]
                amount  = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount > 0:
                    results[service] = round(results.get(service, 0) + amount, 4)
        return {svc: round(amt, 2) for svc, amt in results.items()}
    except Exception as e:
        return {"ERROR": str(e)}


# ─────────────────────────────────────────────
# AWS — Forecast (direct from Cost Explorer)
# ─────────────────────────────────────────────
def fetch_aws_forecast(start: str, end: str) -> float | None:
    try:
        start_dt      = datetime.strptime(start, "%Y-%m-%d").date()
        days_in_month = calendar.monthrange(start_dt.year, start_dt.month)[1]
        month_end     = start_dt.replace(day=days_in_month)
        forecast_end  = str(month_end + timedelta(days=1))
        today_str     = str(utcnow().date())

        client = boto3.client(
            "ce",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
        )
        resp = client.get_cost_forecast(
            TimePeriod={"Start": today_str, "End": forecast_end},
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        forecast_total = float(resp["Total"]["Amount"])
        return forecast_total
    except Exception as e:
        print(f"  ⚠️  AWS forecast API failed ({e}); using linear estimate")
        return None


# ─────────────────────────────────────────────
# Azure — Cost (MTD actual, returns INR)
# ─────────────────────────────────────────────
def fetch_azure_cost(start, end, retries=4, initial_delay=10):
    from azure.identity import ClientSecretCredential
    from azure.mgmt.costmanagement import CostManagementClient
    from azure.mgmt.costmanagement.models import (
        QueryDefinition, QueryTimePeriod, QueryDataset,
        QueryAggregation, QueryGrouping, TimeframeType,
    )

    credential = ClientSecretCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
    )
    client = CostManagementClient(credential)
    scope  = f"/subscriptions/{AZURE_SUBSCRIPTION_ID}"

    query = QueryDefinition(
        type="ActualCost",
        timeframe=TimeframeType.CUSTOM,
        time_period=QueryTimePeriod(
            from_property=start + "T00:00:00Z",
            to=end             + "T00:00:00Z",
        ),
        dataset=QueryDataset(
            granularity="Daily",
            aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
            grouping=[QueryGrouping(type="Dimension", name="ServiceName")],
        ),
    )

    delay = initial_delay
    for attempt in range(1, retries + 1):
        try:
            result = client.query.usage(scope=scope, parameters=query)
            totals = {}
            for row in result.rows:
                amount  = float(row[0])
                service = row[2]
                if amount > 0:
                    totals[service] = round(totals.get(service, 0) + amount, 4)
            raw_total = sum(totals.values())
            print(f"     Azure MTD raw total (INR): {raw_total:,.2f}")
            return {svc: round(amt, 2) for svc, amt in totals.items()}
        except Exception as e:
            if "429" in str(e) and attempt < retries:
                print(f"  ⚠️  Azure rate limit (attempt {attempt}/{retries}), retrying in {delay}s…")
                time.sleep(delay)
                delay *= 2
            else:
                return {"ERROR": str(e)}


# ─────────────────────────────────────────────
# Azure — Forecast (direct from Cost Management API, returns INR)
# ─────────────────────────────────────────────
def fetch_azure_forecast(start: str, end: str, retries=4, initial_delay=10) -> float | None:
    try:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.costmanagement import CostManagementClient
        from azure.mgmt.costmanagement.models import (
            ForecastDefinition, ForecastTimePeriod, ForecastDataset,
            ForecastAggregation, TimeframeType,
        )

        start_dt      = datetime.strptime(start, "%Y-%m-%d").date()
        days_in_month = calendar.monthrange(start_dt.year, start_dt.month)[1]
        month_end_str = f"{start_dt.year}-{start_dt.month:02d}-{days_in_month}"

        credential = ClientSecretCredential(
            tenant_id=AZURE_TENANT_ID,
            client_id=AZURE_CLIENT_ID,
            client_secret=AZURE_CLIENT_SECRET,
        )
        client = CostManagementClient(credential)
        scope  = f"/subscriptions/{AZURE_SUBSCRIPTION_ID}"

        query = ForecastDefinition(
            type="ActualCost",
            timeframe=TimeframeType.CUSTOM,
            time_period=ForecastTimePeriod(
                from_property=start         + "T00:00:00Z",
                to=month_end_str            + "T00:00:00Z",
            ),
            dataset=ForecastDataset(
                granularity="Monthly",
                aggregation={"totalCost": ForecastAggregation(name="Cost", function="Sum")},
            ),
            include_actual_cost=True,
            include_fresh_partial_cost=False,
        )

        delay = initial_delay
        for attempt in range(1, retries + 1):
            try:
                result = client.forecast.usage(scope=scope, parameters=query)
                total = sum(float(row[0]) for row in result.rows if float(row[0]) > 0)
                print(f"     Azure forecast raw (INR): {total:,.2f}")
                return total
            except Exception as e:
                if "429" in str(e) and attempt < retries:
                    print(f"  ⚠️  Azure forecast rate limit (attempt {attempt}/{retries}), retrying in {delay}s…")
                    time.sleep(delay)
                    delay *= 2
                else:
                    raise
    except Exception as e:
        print(f"  ⚠️  Azure forecast API failed ({e}); using linear estimate")
        return None


# ─────────────────────────────────────────────
# GCP — Cost (MTD actual, via BigQuery billing export)
# ─────────────────────────────────────────────
def fetch_gcp_cost(start, end):
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account

        if not GCP_DATASET_TABLE or not GCP_SERVICE_ACCOUNT_JSON:
            return {"ERROR": "GCP_DATASET_TABLE or GCP_SERVICE_ACCOUNT_JSON not configured"}, end

        creds = service_account.Credentials.from_service_account_file(
            GCP_SERVICE_ACCOUNT_JSON,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client = bigquery.Client(credentials=creds, project=creds.project_id)

        end_dt     = datetime.strptime(end, "%Y-%m-%d").date()
        end_padded = str(end_dt + timedelta(days=3))

        max_date_query = f"""
            SELECT MAX(DATE(usage_start_time)) AS max_date
            FROM `{GCP_DATASET_TABLE}`
            WHERE DATE(usage_start_time) >= DATE('{start}')
              AND cost > 0
        """
        max_date_result = list(client.query(max_date_query).result())
        if max_date_result and max_date_result[0].max_date:
            gcp_actual_end = str(max_date_result[0].max_date)
        else:
            gcp_actual_end = end
        print(f"     GCP BQ export — actual data available up to: {gcp_actual_end}")

        query = f"""
            SELECT
                service.description          AS service_name,
                ROUND(SUM(cost), 4)          AS total_cost,
                ANY_VALUE(currency)          AS currency
            FROM
                `{GCP_DATASET_TABLE}`
            WHERE
                DATE(usage_start_time) >= DATE('{start}')
                AND DATE(usage_start_time) <  DATE('{end_padded}')
                AND cost > 0
            GROUP BY
                service.description
            ORDER BY
                total_cost DESC
        """

        results = list(client.query(query).result())

        if not results:
            print("     GCP: query returned 0 rows — no data in BQ for this date range.")
            print(f"          Table queried : {GCP_DATASET_TABLE}")
            print(f"          Date window   : {start} → {end_padded} (padded for BQ lag)")
            return {}, gcp_actual_end

        totals = {}
        for row in results:
            if row.total_cost and row.total_cost > 0:
                totals[row.service_name] = round(row.total_cost, 2)

        raw_total = sum(totals.values())
        print(f"     GCP MTD raw total (billing currency): {raw_total:,.2f}")
        return totals, gcp_actual_end

    except Exception as e:
        return {"ERROR": str(e)}, end


# ─────────────────────────────────────────────
# GCP — Forecast (direct from GCP Budgets API, returns billing currency)
# ─────────────────────────────────────────────
def fetch_gcp_forecast(start: str, end: str) -> float | None:
    try:
        from google.cloud import billing_budgets_v1
        from google.oauth2 import service_account as sa_module

        if not GCP_SERVICE_ACCOUNT_JSON or not GCP_BILLING_ACCOUNT_ID:
            print("  ⚠️  GCP forecast: GCP_SERVICE_ACCOUNT_JSON or GCP_BILLING_ACCOUNT_ID not set; using linear estimate")
            return None

        creds = sa_module.Credentials.from_service_account_file(
            GCP_SERVICE_ACCOUNT_JSON,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

        budget_client = billing_budgets_v1.BudgetServiceClient(credentials=creds)
        parent        = f"billingAccounts/{GCP_BILLING_ACCOUNT_ID}"

        budgets = list(budget_client.list_budgets(parent=parent))

        if not budgets:
            print("     GCP forecast: no budgets found on billing account; using linear estimate")
            return None

        total_forecast = 0.0
        budget_count   = 0

        for budget in budgets:
            try:
                if budget.amount and budget.amount.last_period_amount:
                    continue
                if hasattr(budget, "forecasted_spend") and budget.forecasted_spend:
                    units  = float(budget.forecasted_spend.units or 0)
                    nanos  = float(budget.forecasted_spend.nanos  or 0) / 1e9
                    total_forecast += units + nanos
                    budget_count   += 1
            except Exception:
                continue

        if total_forecast > 0:
            print(f"     GCP forecast (Budgets API, {budget_count} budget(s)): {total_forecast:,.2f}")
            return total_forecast
        else:
            print("     GCP forecast: Budgets API returned no forecasted_spend; using linear estimate")
            return None

    except ImportError:
        print("  ⚠️  GCP forecast: google-cloud-billing-budgets not installed")
        print("       Run: pip install google-cloud-billing-budgets")
        print("       Falling back to linear estimate")
        return None
    except Exception as e:
        print(f"  ⚠️  GCP forecast (Budgets API) failed ({e}); using linear estimate")
        return None


# ─────────────────────────────────────────────
# HTML Builder
# ─────────────────────────────────────────────
AWS_ACCOUNT_ID = "230753728425"

def build_table_rows(data):
    if "ERROR" in data:
        return f'<tr><td colspan="2" style="color:#c0392b;padding:8px 12px">⚠ Error: {data["ERROR"]}</td></tr>'
    if not data:
        return '<tr><td colspan="2" style="padding:8px 12px;color:#888">No cost data found for this period</td></tr>'
    rows = ""
    for service, cost in sorted(data.items(), key=lambda x: -x[1]):
        rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">{service}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;text-align:right;
                     font-weight:600;font-family:monospace">${cost:,.2f}</td>
        </tr>"""
    return rows


def cloud_total_str(data):
    if "ERROR" in data:
        return "Error"
    return f"${sum(data.values()):,.2f}"


def forecast_row(fc: dict, color: str) -> str:
    pct    = fc["pct_month_done"]
    fcast  = fc["forecast"]
    days_e = fc["days_elapsed"]
    days_m = fc["days_in_month"]
    bar_w  = min(int(pct), 100)
    return f"""
    <div style="margin-top:10px;padding:12px 14px;background:#f9f9f9;
                border-radius:6px;border-left:3px solid {color}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-size:12px;color:#555">
          📅 Day {days_e} of {days_m} ({pct}% of month elapsed)
        </span>
        <span style="font-size:13px;font-weight:700;color:{color}">
          Forecast: ${fcast:,.2f}
        </span>
      </div>
      <div style="background:#e0e0e0;border-radius:4px;height:6px;overflow:hidden">
        <div style="width:{bar_w}%;background:{color};height:6px;border-radius:4px"></div>
      </div>
    </div>"""


def month_final_badge(color: str) -> str:
    """Shown instead of the forecast bar when the report is a month-end/final report."""
    return f"""
    <div style="margin-top:10px;padding:10px 14px;background:#f9f9f9;
                border-radius:6px;border-left:3px solid {color}">
      <span style="font-size:12px;font-weight:600;color:{color}">
        ✅ Final total for the month — no forecast (month complete)
      </span>
    </div>"""


def note_block(note: str) -> str:
    if not note or not note.strip():
        return ""
    return f"""
    <div style="margin-top:20px;padding:14px 16px;background:#f0ad00;
                border-radius:6px">
      <div style="font-weight:600;margin-bottom:6px;color:#ffffff">📌 Note</div>
      <div style="font-size:13px;color:#ffffff;line-height:1.6">{note.strip()}</div>
    </div>"""


def section(cloud_name, color, data, date_label, fc: dict | None, is_month_end: bool) -> str:
    total_str = cloud_total_str(data)

    if "ERROR" in data:
        footer_html = ""
    elif fc is not None:
        footer_html = forecast_row(fc, color)
    elif is_month_end:
        footer_html = month_final_badge(color)
    else:
        footer_html = ""

    return f"""
    <h3 style="margin:28px 0 4px;color:{color};font-size:16px">{cloud_name} — {total_str}</h3>
    <p style="margin:0 0 8px;font-size:14px;font-weight:700;color:#222">{date_label}</p>
    <table style="width:100%;border-collapse:collapse;background:#ffffff;
                  border-radius:8px;overflow:hidden;
                  box-shadow:0 1px 3px rgba(0,0,0,0.08)">
      <thead>
        <tr style="background:{color};color:#ffffff">
          <th style="padding:10px 12px;text-align:left;font-weight:500;font-size:13px">Service</th>
          <th style="padding:10px 12px;text-align:right;font-weight:500;font-size:13px">Cost (USD)</th>
        </tr>
      </thead>
      <tbody>{build_table_rows(data)}</tbody>
    </table>
    {footer_html}"""


def report_title(start: str, is_month_end: bool) -> str:
    """'June 2026 Report (Final)' for month-end runs, plain date range otherwise."""
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    if is_month_end:
        return f"{start_dt.strftime('%B %Y')} Report (Final)"
    return None  # signals caller to use the date-range label instead


def build_html(aws, azure, gcp, start, end, is_month_end: bool,
               fc_aws=None, fc_azure=None, fc_gcp=None,
               gcp_actual_end=None):

    total_aws   = sum(aws.values())   if "ERROR" not in aws   else 0.0
    total_azure = sum(azure.values()) if "ERROR" not in azure else 0.0
    total_gcp   = sum(gcp.values())   if "ERROR" not in gcp   else 0.0

    # Only fill in a default forecast if the caller didn't already decide
    # to skip it (month-end) and didn't pass one in.
    if fc_aws is None and not is_month_end:
        fc_aws = compute_forecast(total_aws, start, end)
    if fc_azure is None and not is_month_end:
        fc_azure = compute_forecast(total_azure, start, end)
    if fc_gcp is None and not is_month_end:
        fc_gcp = compute_forecast(total_gcp, start, end)

    days_elapsed_label = fc_aws['days_elapsed'] if fc_aws else (
        datetime.strptime(end, "%Y-%m-%d").date() - datetime.strptime(start, "%Y-%m-%d").date()
    ).days

    month_title = report_title(start, is_month_end)
    aws_azure_date_label = (
        month_title if month_title
        else f"{start} → {end} (day 1–{days_elapsed_label} of month)"
    )

    gcp_end_display = gcp_actual_end or end
    gcp_date_label = (
        month_title if month_title
        else f"{start} → {gcp_end_display} (BQ export lag — data available up to {gcp_end_display})"
    )

    generated = utcnow().strftime("%Y-%m-%d %H:%M")

    AWS_COLOR   = "#E8811A"
    AZURE_COLOR = "#0072C6"
    GCP_COLOR   = "#1E8E3E"

    header_subtitle = month_title if month_title else f"{start} → {end}"

    def forecast_line(fc):
        return f"Forecast ${fc['forecast']:,.2f}" if fc else ("Month closed" if is_month_end else "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;font-family:'Segoe UI',Arial,sans-serif;
             background-color:#ffffff;-webkit-text-size-adjust:100%">

  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:#ffffff;padding:32px 16px">
    <tr><td align="center">

      <!-- Card -->
      <table width="640" cellpadding="0" cellspacing="0" border="0"
             style="max-width:640px;background-color:#ffffff;border-radius:10px;
                    overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.10)">

        <!-- Header strip -->
        <tr>
          <td style="background:linear-gradient(135deg,#1a1f36 0%,#2d3561 100%);
                     padding:24px 28px">
            <div style="font-size:22px;font-weight:700;color:#ffffff">
              ☁️ Cloud Cost Report
            </div>
            <div style="font-size:12px;color:#a0aabf;margin-top:4px">
              Generated on {generated} UTC &nbsp;·&nbsp; {header_subtitle}
            </div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:24px 28px">

            <!-- Summary cards -->
            <table width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="margin-bottom:20px">
              <tr>
                <!-- AWS card -->
                <td width="32%" style="background:{AWS_COLOR};border-radius:8px;
                                       padding:14px 10px;text-align:center">
                  <div style="font-size:11px;color:#ffe0c0;margin-bottom:2px;
                               text-transform:uppercase;letter-spacing:.5px">AWS (USD)</div>
                  <div style="font-size:10px;color:#ffe0c0;margin-bottom:4px;
                               letter-spacing:.3px">{AWS_ACCOUNT_ID}</div>
                  <div style="font-size:20px;font-weight:700;color:#ffffff">${total_aws:,.2f}</div>
                  <div style="font-size:11px;color:#ffe0c0;margin-top:2px">{forecast_line(fc_aws)}</div>
                </td>
                <td width="2%"></td>
                <!-- Azure card -->
                <td width="32%" style="background:{AZURE_COLOR};border-radius:8px;
                                       padding:14px 10px;text-align:center">
                  <div style="font-size:11px;color:#d0e8ff;margin-bottom:4px;
                               text-transform:uppercase;letter-spacing:.5px">Azure (USD)</div>
                  <div style="font-size:20px;font-weight:700;color:#ffffff">${total_azure:,.2f}</div>
                  <div style="font-size:11px;color:#d0e8ff;margin-top:2px">{forecast_line(fc_azure)}</div>
                </td>
                <td width="2%"></td>
                <!-- GCP card -->
                <td width="32%" style="background:{GCP_COLOR};border-radius:8px;
                                       padding:14px 10px;text-align:center">
                  <div style="font-size:11px;color:#c0f0d0;margin-bottom:4px;
                               text-transform:uppercase;letter-spacing:.5px">GCP (USD)</div>
                  <div style="font-size:20px;font-weight:700;color:#ffffff">${total_gcp:,.2f}</div>
                  <div style="font-size:11px;color:#c0f0d0;margin-top:2px">{forecast_line(fc_gcp)}</div>
                </td>
              </tr>
            </table>

            {section(f"AWS ({AWS_ACCOUNT_ID})", AWS_COLOR,   aws,   aws_azure_date_label, fc_aws,   is_month_end)}
            {section("Azure",                   AZURE_COLOR, azure, aws_azure_date_label, fc_azure, is_month_end)}
            {section("GCP",                     GCP_COLOR,   gcp,   gcp_date_label,       fc_gcp,   is_month_end)}

            {note_block(EMAIL_NOTE)}

            <!-- Footer -->
            <p style="margin-top:28px;font-size:11px;color:#bbb;text-align:center;
                      border-top:1px solid #f0f0f0;padding-top:16px">
              Auto-generated by Cloud Cost Reporter &nbsp;·&nbsp;
              {generated} UTC
            </p>

          </td>
        </tr>
      </table>

    </td></tr>
  </table>

</body></html>"""


# ─────────────────────────────────────────────
# Graph API Email
# ─────────────────────────────────────────────
def get_access_token() -> str:
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         "https://graph.microsoft.com/.default",
    }
    response = requests.post(url, data=payload)
    response.raise_for_status()
    print("✅ Token fetched")
    return response.json()["access_token"]


def send_email(token: str, html: str, start: str, end: str, is_month_end: bool):
    url = f"https://graph.microsoft.com/v1.0/users/{FROM_EMAIL}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    month_title = report_title(start, is_month_end)
    if month_title:
        subject = f"☁️ Cloud Cost Report | {month_title} "
    else:
        subject = f"☁️ Cloud Cost Report | {start} to {end}"

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": html,
            },
            "toRecipients": [
                {"emailAddress": {"address": email}} for email in TO_EMAIL
            ],
            "ccRecipients": [
                {"emailAddress": {"address": email}} for email in CC_EMAIL
            ],
        },
        "saveToSentItems": "true",
    }
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    print(f"✅ Email sent to {TO_EMAIL}, cc: {CC_EMAIL}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    start, end, is_month_end = get_date_range()
    print(f"📅 Reporting period : {start} → {end}  (REPORT_DAY={REPORT_DAY}, month_end={is_month_end})")

    inr_to_usd = get_inr_to_usd_rate()

    # ── AWS ──────────────────────────────────────────────────────────────
    print("\n  → AWS (MTD actual) ...")
    aws = fetch_aws_cost(start, end)
    if "ERROR" not in aws:
        total_aws = sum(aws.values())
        print(f"     AWS total : ${total_aws:,.2f}")

        if is_month_end:
            fc_aws = None
            print("     AWS forecast skipped (month-end report)")
        else:
            print("  → AWS forecast ...")
            aws_forecast_raw = fetch_aws_forecast(start, end)
            if aws_forecast_raw is not None:
                print(f"     AWS forecast (CE API): ${aws_forecast_raw:,.2f}")
                fc_aws = compute_forecast(total_aws, start, end,
                                          forecast_override=aws_forecast_raw)
            else:
                fc_aws = compute_forecast(total_aws, start, end)
                print(f"     AWS forecast (linear): ${fc_aws['forecast']:,.2f}")
    else:
        print(f"     AWS error : {aws['ERROR']}")
        fc_aws = None

    # ── Azure ─────────────────────────────────────────────────────────────
    print("\n  → Azure (MTD actual) ...")
    azure_raw = fetch_azure_cost(start, end)
    azure     = convert_inr_dict_to_usd(azure_raw, inr_to_usd)
    if "ERROR" not in azure:
        total_azure = sum(azure.values())
        print(f"     Azure total (USD) : ${total_azure:,.2f}")

        if is_month_end:
            fc_azure = None
            print("     Azure forecast skipped (month-end report)")
        else:
            print("  → Azure forecast ...")
            azure_fc_inr = fetch_azure_forecast(start, end)
            if azure_fc_inr is not None:
                azure_fc_usd = round(azure_fc_inr * inr_to_usd, 2)
                print(f"     Azure forecast (USD) : ${azure_fc_usd:,.2f}")
                fc_azure = compute_forecast(total_azure, start, end,
                                            forecast_override=azure_fc_usd)
            else:
                fc_azure = compute_forecast(total_azure, start, end)
                print(f"     Azure forecast (linear): ${fc_azure['forecast']:,.2f}")
    else:
        print(f"     Azure error : {azure['ERROR']}")
        fc_azure = None

    # ── GCP ───────────────────────────────────────────────────────────────
    print("\n  → GCP (MTD actual) ...")
    gcp_start, gcp_end, _ = get_gcp_date_range()  # same is_month_end as above; ignore 2nd copy

    gcp_raw, gcp_actual_end = fetch_gcp_cost(gcp_start, gcp_end)
    gcp = convert_inr_dict_to_usd(gcp_raw, inr_to_usd)

    if "ERROR" not in gcp:
        if gcp:
            total_gcp = sum(gcp.values())
            print(f"     GCP total (USD) : ${total_gcp:,.2f}")

            if is_month_end:
                fc_gcp = None
                print("     GCP forecast skipped (month-end report)")
            else:
                print("  → GCP forecast (Budgets API) ...")
                gcp_fc_inr = fetch_gcp_forecast(gcp_start, gcp_end)
                if gcp_fc_inr is not None:
                    gcp_fc_usd = round(gcp_fc_inr * inr_to_usd, 2)
                    print(f"     GCP forecast (USD) : ${gcp_fc_usd:,.2f}")
                    fc_gcp = compute_forecast(total_gcp, start, end,
                                              forecast_override=gcp_fc_usd)
                else:
                    fc_gcp = compute_forecast(total_gcp, start, end)
                    print(f"     GCP forecast (linear): ${fc_gcp['forecast']:,.2f}")
        else:
            print("     GCP : no data found (check BQ table / date range)")
            fc_gcp = None
    else:
        print(f"     GCP error : {gcp['ERROR']}")
        fc_gcp = None

    print("\n  → Building HTML ...")
    html = build_html(
        aws, azure, gcp, start, end, is_month_end,
        fc_aws=fc_aws,
        fc_azure=fc_azure,
        fc_gcp=fc_gcp,
        gcp_actual_end=gcp_actual_end,
    )

    print("  → Sending email ...")
    token = get_access_token()
    send_email(token, html, start, end, is_month_end)

    print("\n🎉 Done!")